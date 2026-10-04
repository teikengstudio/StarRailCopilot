"""极验 (aigis) 风控挑战的"浏览器代解"辅助。

背景
----
密码登录触发风控时, ``loginByPassword`` 返回 ``retcode=-3101``, 响应头
``x-rpc-aigis`` 给出一个**绑定本次会话**的极验挑战::

    {"session_id": "<sid>", "data": "<JSON 字符串: 极验 initGeetest 配置>"}

登录页 JS 的处理是 (``login-platform/js/5823.9f588959.js`` 模块 49208 +
axios 响应拦截器):

1. ``JSON.parse(x-rpc-aigis)`` → ``{session_id, data}``, 再 ``JSON.parse(data)``;
2. v4: ``initGeetest4({captchaId: gt, riskType: risk_type, language:"zho",
   product:"bind", timeout:1e4, hideSuccess:true,
   userInfo: JSON.stringify({session_id})}, cb)``, 成功后 ``captcha.getValidate()``;
3. 重发原请求, 头 ``x-rpc-aigis = session_id + ";" + btoa(JSON.stringify(validate))``。

本模块把这套流程搬到一个**本机一次性网页**: 用户在浏览器里手动完成滑块
(极验就是要人来做), 页面把结果回传给本机脚本, 于是 ``-3101`` 不再需要人工
拼 base64。库不参与、也不伪造验证码结果。

用法::

    from core.aigis import browser_aigis_solver

    cookie = auth.login_password(
        account, password,
        on_aigis=browser_aigis_solver(),   # 回调收到 challenge, 返回 x-rpc-aigis 头值
    )

安全约定:
    * 只监听 ``127.0.0.1``, 路径带一次性随机 token, 不接受其它来源;
    * 收到的头值必须与本次下发 ``session_id`` 前缀一致, 否则丢弃;
    * 结果为一次性, 收到即关站退出。
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .log import get_logger

# 与抓包一致的极验组件地址 (登录页实际加载的那个文件, v4.1.6)。
GEETEST_V4_JS_URL = "https://webstatic.mihoyo.com/dora/lib/geetest/v4/gt4.js"
# v3 挑战 (data.use_v4 为假) 的兜底组件; 抓包里没有出现, 仅作兼容。
GEETEST_V3_JS_URL = "https://static.geetest.com/static/js/gt.0.5.0.js"

MAX_BODY_BYTES = 16 * 1024


class AigisSolveError(RuntimeError):
    """浏览器代解失败 (超时、回传被拒、组件加载失败等)。"""


class AigisTimeout(AigisSolveError):
    """等待用户完成验证码超时。"""


def _js_json(value: Any) -> str:
    """把 Python 对象嵌进 ``<script>`` 的安全 JSON 字面量。"""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_solver_page(
    challenge: Mapping[str, Any],
    *,
    post_path: str,
    gt4_url: str = GEETEST_V4_JS_URL,
    v3_url: str = GEETEST_V3_JS_URL,
) -> str:
    """渲染代解页面。

    参数:
        challenge: :func:`core.auth.parse_aigis_header` 的结果
            (``{"session_id", "data", "raw"}``); ``data`` 是极验 init 配置。
        post_path: 浏览器回传结果的相对路径 (含一次性 token)。
        gt4_url: 极验 v4 组件地址。
        v3_url: 极验 v3 组件地址 (仅 ``use_v4`` 为假时动态加载)。

    返回:
        完整 HTML 文本。所有注入值都经过 JSON 转义, 不会破坏 ``<script>``。
    """
    session_id = str(challenge.get("session_id") or "")
    config = challenge.get("data")
    if not isinstance(config, Mapping):
        # data 解析失败时把它原样交给页面, 至少让用户看到挑战内容
        config = {"raw": config}
    return _PAGE_TEMPLATE.replace("__CONFIG__", _js_json(dict(config))).replace(
        "__SESSION_ID__", _js_json(session_id)
    ).replace("__POST_URL__", _js_json(post_path)).replace(
        "__GT4_URL__", gt4_url
    ).replace("__V3_URL__", _js_json(v3_url))


class BrowserAigisSolver:
    """本机一次性 HTTP 服务: 浏览器解验证码 → 回传 ``x-rpc-aigis`` 头值。

    典型用法 (CLI 由 :func:`browser_aigis_solver` 封装)::

        solver = BrowserAigisSolver(timeout=180, on_status=print)
        with solver:
            url = solver.start(challenge)   # 打印/打开这个 URL
            header = solver.wait()          # 阻塞直到用户完成或超时
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        timeout: float = 180.0,
        open_browser: bool = True,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = float(timeout)
        self.open_browser = open_browser
        self.logger = get_logger("aigis")
        self._on_status = on_status

        self.token = secrets.token_urlsafe(24)
        self.session_id = ""
        self._header_value: str | None = None
        self._rejected: str | None = None
        self._done = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- 生命周期 -----------------------------------------------------
    def __enter__(self) -> "BrowserAigisSolver":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def page_url(self) -> str:
        """给用户打开的页面地址。"""
        return f"http://{self.host}:{self.port}/{self.token}/"

    @property
    def result_url(self) -> str:
        """浏览器回传结果用的相对路径。"""
        return f"/{self.token}/result"

    def _report(self, message: str) -> None:
        if self._on_status is not None:
            self._on_status(message)
        else:
            self.logger.info(message)

    def start(self, challenge: Mapping[str, Any]) -> str:
        """起服务并返回页面 URL (不阻塞)。"""
        session_id = str(challenge.get("session_id") or "")
        if not session_id:
            raise AigisSolveError("x-rpc-aigis 缺少 session_id, 无法发起人工验证")
        self.session_id = session_id

        page = render_solver_page(challenge, post_path=self.result_url).encode("utf-8")
        solver = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AigisSolver/1.0"

            def log_message(self, *_args: object) -> None:  # 静音访问日志
                return

            def _plain(self, code: int, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定)
                if urlparse(self.path).path != f"/{solver.token}/":
                    self._plain(404, "not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path != solver.result_url:
                    self._plain(404, "not found")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._plain(400, "bad length")
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self._plain(400, "bad json")
                    return
                header_value = str((payload or {}).get("header") or "")
                prefix, _, encoded = header_value.partition(";")
                if prefix != solver.session_id or not encoded:
                    solver._rejected = "回传的 session_id 与本次挑战不一致"
                    self._plain(400, "session mismatch")
                    return
                solver._header_value = header_value
                solver._done.set()
                self._plain(200, json.dumps({"ok": True}))

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="aigis-solver", daemon=True
        )
        self._thread.start()
        return self.page_url

    def wait(self) -> str:
        """阻塞直到收到结果 (返回 ``x-rpc-aigis`` 头值) 或超时。"""
        if self._done.wait(self.timeout) and self._header_value:
            return self._header_value
        if self._rejected:
            raise AigisSolveError(self._rejected)
        raise AigisTimeout(f"等待人工完成验证码超时 ({self.timeout:.0f}s)")

    def close(self) -> None:
        """关闭本地服务 (幂等)。"""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def solve(self, challenge: Mapping[str, Any]) -> str:
        """完整跑一次: 起服务 → 提示 / 打开浏览器 → 等结果 → 关服务。"""
        url = self.start(challenge)
        try:
            self._report("[风控] 检测到极验验证码, 需要在浏览器里人工完成。")
            self._report(f"       请打开: {url}")
            if self.host in ("127.0.0.1", "localhost"):
                self._report(
                    f"       (远程/容器里跑的话, 用 ssh -L {self.port}:127.0.0.1:{self.port} 转发后在本机浏览器打开)"
                )
            if self.open_browser:
                try:
                    webbrowser.open(url)
                except Exception:  # 无 GUI / 无默认浏览器时不致命
                    pass
            self._report(f"       完成后会自动回传 (超时 {self.timeout:.0f}s), 也可 Ctrl+C 中止")
            header = self.wait()
            self._report("       已收到验证结果, 重试登录 ...")
            return header
        finally:
            self.close()


def browser_aigis_solver(**kwargs: Any) -> Callable[[dict[str, Any]], str]:
    """返回可直接传给 ``Authenticator.login_password(on_aigis=...)`` 的回调。

    ``kwargs`` 透传 :class:`BrowserAigisSolver` (``timeout`` / ``open_browser`` /
    ``on_status`` / ``host`` / ``port``)。每次唤起都新建一次性服务。
    """

    def _solve(challenge: dict[str, Any]) -> str:
        with BrowserAigisSolver(**kwargs) as solver:
            return solver.solve(challenge)

    return _solve


_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>登录风控验证</title>
<style>
  body { font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #16161a; color: #e8e8ea; margin: 0; padding: 24px; }
  .wrap { max-width: 520px; margin: 0 auto; }
  h1 { font-size: 18px; margin: 0 0 8px; }
  p { color: #b9b9c2; font-size: 14px; line-height: 1.6; }
  #captcha { margin: 16px 0; min-height: 64px; }
  pre { background: #0d0d10; border: 1px solid #2a2a31; border-radius: 8px;
        padding: 12px; white-space: pre-wrap; word-break: break-all; font-size: 12px; }
  .ok { color: #7bd88f; } .err { color: #ff8a8a; }
  code { background: #0d0d10; padding: 1px 4px; border-radius: 4px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>米哈游登录风控验证</h1>
  <p>请在下方完成滑块验证。验证通过后本页会把结果自动回传给本机登录脚本，
     随后即可关闭本页。</p>
  <div id="captcha"></div>
  <pre id="status">正在加载极验组件…</pre>
  <details>
    <summary>自动回传失败？点这里手动兜底</summary>
    <p>把下面这行完整复制到命令行的 <code>--aigis</code> 参数里：</p>
    <pre id="manual">（验证完成后出现）</pre>
  </details>
</div>
<script src="__GT4_URL__"></script>
<script>
const CONFIG = __CONFIG__;
const SESSION_ID = __SESSION_ID__;
const POST_URL = __POST_URL__;
const V3_URL = __V3_URL__;
const statusEl = document.getElementById('status');
const manualEl = document.getElementById('manual');

function setStatus(text, cls) { statusEl.textContent = text; statusEl.className = cls || ''; }
function b64(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  bytes.forEach(b => { bin += String.fromCharCode(b); });
  return btoa(bin);
}
function finish(validate) {
  const header = SESSION_ID + ';' + b64(JSON.stringify(validate));
  manualEl.textContent = header;
  fetch(POST_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ header: header })
  }).then(r => r.json()).then(r => {
    if (r && r.ok) { setStatus('验证结果已回传，请回到终端继续（本页可关闭）。', 'ok'); }
    else { setStatus('回传被拒绝，请把下方内容复制到 --aigis。', 'err'); }
  }).catch(() => {
    setStatus('无法回传（脚本可能已退出），请把下方内容复制到 --aigis。', 'err');
  });
}
function fail(error) { setStatus('验证失败或被取消：' + JSON.stringify(error || {}), 'err'); }
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error('加载失败: ' + src));
    document.head.appendChild(el);
  });
}
async function boot() {
  if (CONFIG.use_v4) {
    if (typeof initGeetest4 !== 'function') {
      setStatus('极验 v4 组件未加载（网络受限？），请改用扫码登录或在登录页手动完成。', 'err');
      return;
    }
    initGeetest4({
      captchaId: CONFIG.gt,
      riskType: CONFIG.risk_type,
      timeout: 10000,
      language: 'zho',
      product: 'bind',
      hideSuccess: true,
      userInfo: JSON.stringify({ session_id: SESSION_ID }),
      onError: fail
    }, function (captcha) {
      captcha.onReady(function () { captcha.showCaptcha(); });
      captcha.onSuccess(function () { finish(captcha.getValidate()); });
      captcha.onError(fail);
      captcha.onClose(function () { fail({ error_type: 'close' }); });
    });
  } else {
    await loadScript(V3_URL);
    if (typeof initGeetest !== 'function') {
      setStatus('极验 v3 组件未加载，请改用扫码登录。', 'err');
      return;
    }
    initGeetest({
      width: '100%', lang: 'zh-cn', timeout: 10000,
      api_server: 'apiv6.geetest.com',
      gt: CONFIG.gt, challenge: CONFIG.challenge, new_captcha: CONFIG.new_captcha,
      product: 'bind', offline: !CONFIG.success, onError: fail
    }, function (captcha) {
      captcha.onReady(function () { captcha.verify(); });
      captcha.onSuccess(function () { finish(captcha.getValidate()); });
      captcha.onError(fail);
      captcha.onClose(function () { fail({ error_type: 'close' }); });
    });
  }
}
boot().catch(e => setStatus('初始化失败：' + e.message, 'err'));
</script>
</body>
</html>
"""


__all__ = [
    "AigisSolveError",
    "AigisTimeout",
    "BrowserAigisSolver",
    "GEETEST_V3_JS_URL",
    "GEETEST_V4_JS_URL",
    "browser_aigis_solver",
    "render_solver_page",
]
