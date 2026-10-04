"""米哈游通行证 (passport-api) 凭据管理。

本模块承载所有"通行证 SDK"相关的常量、纯工具函数与登录类，包括:

* 共享常量与请求头模板 —— 供 ``core.dispatcher`` 等模块复用 (但 dispatcher
  仅 import 工具函数, 不会引用 :class:`Authenticator`, 保持职责解耦)。
* :func:`parse_cookie_header` 等纯函数 —— 无副作用, 任何模块可直接调用。
* :class:`Authenticator` —— 二维码扫码登录 + 账号密码登录 + cookie 有效性
  校验, 负责 ``credentials.json`` 的读写。

设计要点:
    Dispatcher 自带 ``webVerifyForGame`` 调用是为了换 ``channel_token`` 走
    后续调度链, 与本模块 ``Authenticator.check`` 的"探活"语义不同, 因此
    两边 *不共享* 状态, 仅共享底层常量与 header 构造函数。

密码登录协议还原依据 (``cloudgame.har`` 第 104 条, 2026-09-30 18:17:20):
    ``POST https://passport-api.mihoyo.com/account/ma-cn-passport/web/loginByPassword``
    请求体 ``{"account": <base64>, "password": <base64>}`` —— 两个字段都是
    **RSA-1024 / PKCS#1 v1.5** 密文的 base64 (密文恒为 128 字节 = 172 字符,
    抓包中 Content-Length=372 与两个 172 字符字段逐字节吻合)。
    公钥来自登录页 JS ``login-platform/js/5823.9f588959.js`` 模块 38625
    (``new JSEncrypt({}); setPublicKey(<RSA_PUBLIC_KEY_B64>)``, 被
    ``login.35d0ab6d.js`` 里的 API 封装 ``require``); 同一模块的
    API 封装对 ``account`` / ``password`` 调用 ``RSA.encrypt()``; JSEncrypt
    3.2.1 的 ``encrypt`` 即 PKCS#1 v1.5 填充 + ``hex2b64``。请求头带
    ``x-rpc-source: v2.webLogin``、``x-rpc-client_type`` 为登录页 platform、
    ``x-rpc-mi_referrer`` 指向 ``#/login/password``。响应 ``Set-Cookie``
    下发 ``cookie_token_v2`` / ``ltoken_v2`` / ``account_mid_v2`` 等必需字段。
    风控挑战 (retcode ``-3101``) 时响应头 ``x-rpc-aigis`` 携带极验会话,
    需要人工完成验证码后以 ``x-rpc-aigis: <session_id>;<base64(result)>`` 重试。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event
from typing import Any

import requests

from .log import get_logger


# ---------------------------------------------------------------------------
# 端点 / SDK 标识
# ---------------------------------------------------------------------------
PASSPORT_BASE = "https://passport-api.mihoyo.com"
CREATE_QR_URL = f"{PASSPORT_BASE}/account/ma-cn-passport/web/createQRLogin"
QUERY_QR_URL = f"{PASSPORT_BASE}/account/ma-cn-passport/web/queryQRLoginStatus"
PASSWORD_LOGIN_URL = f"{PASSPORT_BASE}/account/ma-cn-passport/web/loginByPassword"
WEB_VERIFY_URL = f"{PASSPORT_BASE}/account/ma-cn-session/web/webVerifyForGame"

PASSPORT_APP_ID = "c90mr1bwo2rk"
GAME_BIZ = "hkrpg_cn"
PASSPORT_SDK_VERSION = "2.53.1"
SESSION_SDK_VERSION = "2.50.1"  # webVerifyForGame 用的版本号略低
LOGIN_PLATFORM_SDK_VERSION = "2.57.0"  # 登录页 (user.mihoyo.com) 自己上报的版本

# 登录页 JS (webpack 模块 8612) 对密码登录强制注入的调用来源; 实现照抄抓包。
X_RPC_SOURCE_WEB_LOGIN = "v2.webLogin"

# 密码 / 手机号 / 身份证等字段的 RSA 公钥 (登录页 JS 硬编码, DER SubjectPublicKeyInfo
# 的 base64, 去掉 PEM 头尾)。JSEncrypt 的 setPublicKey 支持这种无装甲形式。
# 规格: RSA-1024, e=65537; 密文恒 128 字节 → base64 172 字符。
RSA_PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDDvekdPMHN3AYhm/vktJT+YJr7"
    "cI5DcsNKqdsx5DZX0gDuWFuIjzdwButrIYPNmRJ1G8ybDIF7oDW2eEpm5sMbL9zs"
    "9ExXCdvqrn51qELbqj0XxtMTIpaCHFSI50PfPpTFV9Xt/hmyVwokoOXFlAEgCn+Q"
    "CgGs52bFoYMtyi+xEQIDAQAB"
)

# ---------------------------------------------------------------------------
# 通行证 retcode (摘自登录页 JS 的枚举, 与官方前端逐值一致)
# ---------------------------------------------------------------------------
RETCODE_TOKEN_INVALID = -100
RETCODE_ACTION_TICKET_INVALID = -3003
RETCODE_REQUEST_FREQUENCY_LIMIT = -3006
RETCODE_NEED_AIGIS = -3101  # 需要极验验证码, 响应头 x-rpc-aigis 给出会话
RETCODE_ACCOUNT_PWD_TRY_TOO_MUCH = -3208
RETCODE_ACCOUNT_RISKY = -3235
RETCODE_ACCOUNT_SAFELY_FORBIDDEN = -3254
RETCODE_ACCOUNT_SELF_LOGIN_RESTRICTION = -3257
RETCODE_ACCOUNT_LOCKED = -4400

PASSWORD_LOGIN_HINTS: dict[int, str] = {
    RETCODE_NEED_AIGIS: (
        "触发极验风控: 需要在浏览器完成验证码, 再用 build_aigis_header() 的结果"
        " 作为 x-rpc-aigis 重试"
    ),
    RETCODE_ACCOUNT_PWD_TRY_TOO_MUCH: "账号或密码错误次数过多, 请稍后再试或改用扫码登录",
    RETCODE_ACCOUNT_RISKY: "账号命中风控, 建议改用扫码登录",
    RETCODE_ACCOUNT_SAFELY_FORBIDDEN: "账号处于安全限制状态, 请到通行证页面处理后再登录",
    RETCODE_ACCOUNT_SELF_LOGIN_RESTRICTION: "账号被限制自主登录, 请到通行证页面处理",
    RETCODE_ACCOUNT_LOCKED: "账号已被锁定, 请到通行证页面解锁",
    RETCODE_REQUEST_FREQUENCY_LIMIT: "请求过于频繁, 请稍后再试",
    RETCODE_TOKEN_INVALID: "登录状态失效, 请重新登录",
}

# ---------------------------------------------------------------------------
# 浏览器特征 (与 web-SDK 抓包一致)
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Not:A-Brand";v="99", "Google Chrome";v="149", "Chromium";v="149"'

# 登录页 URL 的公共前缀: 除末尾 hash 路由外, 扫码 (#/login/qr) 与密码
# (#/login/password) 完全一致。client_type=25 对应本项目的 Linux 桌面画像
# (CloudWebKeyboard); 抓包是 Windows 10 + Edge, 对应 22 (CloudWebPC)。
LOGIN_PLATFORM_URL = (
    "https://user.mihoyo.com/login-platform/index.html"
    "?client_type=25&app_id=c90mr1bwo2rk&theme=rpg&token_type=4&game_biz=hkrpg_cn"
    "&message_origin=https%253A%252F%252Fsr.mihoyo.com"
    "&succ_back_type=message%253Alogin-platform%253Alogin-success"
    "&fail_back_type=message%253Alogin-platform%253Alogin-fail"
    "&ux_mode=popup&iframe_level=1&extra_trace=1"
)
PASSPORT_REFERER = LOGIN_PLATFORM_URL + "#/login/qr"
PASSWORD_REFERER = LOGIN_PLATFORM_URL + "#/login/password"
# 浏览器对跨源 XHR 只发 origin 形式的 Referer (strict-origin-when-cross-origin),
# 抓包里 loginByPassword 的 Referer 正是 "https://user.mihoyo.com/";
# 完整登录页 URL 只出现在 SDK 自己注入的 x-rpc-mi_referrer 里。
LOGIN_ORIGIN = "https://user.mihoyo.com/"
CLOUD_REFERER = "https://sr.mihoyo.com/cloud/"

# ---------------------------------------------------------------------------
# 业务常量
# ---------------------------------------------------------------------------
REQUIRED_COOKIES: tuple[str, ...] = ("cookie_token_v2", "account_mid_v2")
TERMINAL_FAIL_STATUSES: frozenset[str] = frozenset(
    {"Expired", "Failed", "Disabled", "Cancel", "Cancelled"}
)


# ---------------------------------------------------------------------------
# 凭据文件 IO
# ---------------------------------------------------------------------------
def load_cookie(path: Path | str, *, logger: logging.Logger | None = None) -> str | None:
    """从指定 JSON 文件读取 ``cookie`` 字段。

    调用方必须显式传入路径。文件缺失、读取失败或字段缺失时返回 ``None``；
    如果文件中显式写了 ``"cookie": ""``，则返回空字符串。
    """
    cookie_path = Path(path)
    if not cookie_path.exists():
        return None
    try:
        data = json.loads(cookie_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        (logger or get_logger("auth")).warning("读取 %s 失败: %s", cookie_path, exc)
        return None
    if "cookie" not in data:
        return None
    return str(data.get("cookie") or "")


def save_cookie(path: Path | str, cookie: str, *, backup: bool = True, logger: logging.Logger | None = None) -> Path:
    """把 cookie 写入指定 JSON 文件；已存在时可按时间戳生成 ``.bak``。"""
    cookie_path = Path(path)
    if cookie_path.exists() and backup:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = cookie_path.with_suffix(cookie_path.suffix + f".bak.{timestamp}")
        backup_path.write_bytes(cookie_path.read_bytes())
        (logger or get_logger("auth")).info("原文件已备份: %s", backup_path)
    payload = {"cookie": cookie}
    cookie_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return cookie_path


# ---------------------------------------------------------------------------
# 纯工具函数 —— Dispatcher 等模块可直接 import 复用
# ---------------------------------------------------------------------------
def parse_cookie_header(text: str) -> dict[str, str]:
    """把单行 ``Cookie`` header 拆成 dict, 值会做 URL 解码。"""
    from urllib.parse import unquote_plus

    out: dict[str, str] = {}
    for part in (text or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        out[key.strip()] = unquote_plus(value.strip())
    return out


def is_placeholder(value: str) -> bool:
    """check_cookies.py 里的占位符约定: 含 ``*`` 视为打码后的不可用值。"""
    return "*" in value


def gen_device_fp() -> str:
    """生成 13 位十六进制 ``DEVICEFP`` (与浏览器 SDK 字符集/长度一致)。"""
    return secrets.token_hex(7)[:13]


def gen_lifecycle_id() -> str:
    """通行证 SDK 每次启动随机生成的 10 位十六进制 lifecycle id。"""
    return secrets.token_hex(5)


def make_passport_headers(device_id: str, device_fp: str, lifecycle_id: str) -> dict[str, str]:
    """通行证 SDK (createQRLogin / queryQRLoginStatus) 通用请求头。"""
    return {
        "user-agent": USER_AGENT,
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://user.mihoyo.com",
        "referer": PASSPORT_REFERER,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "x-rpc-app_id": PASSPORT_APP_ID,
        "x-rpc-client_type": "25",
        "x-rpc-device_id": device_id,
        "x-rpc-device_fp": device_fp,
        "x-rpc-device_model": "Chrome%20149.0.0.0",
        "x-rpc-device_name": "Chrome",
        "x-rpc-device_os": "Linux%2064-bit",
        "x-rpc-game_biz": GAME_BIZ,
        "x-rpc-language": "zh-cn",
        "x-rpc-lifecycle_id": lifecycle_id,
        "x-rpc-mi_referrer": PASSPORT_REFERER,
        "x-rpc-sdk_version": PASSPORT_SDK_VERSION,
    }


def make_verify_headers(base: dict[str, str]) -> dict[str, str]:
    """``webVerifyForGame`` 的请求头 (来源切换到云游戏页, sdk_version 不同)。"""
    headers = dict(base)
    headers.update(
        {
            "origin": "https://sr.mihoyo.com",
            "referer": CLOUD_REFERER,
            "x-rpc-mi_referrer": CLOUD_REFERER,
            "x-rpc-sdk_version": SESSION_SDK_VERSION,
            "x-rpc-app_version": "",
        }
    )
    return headers


def make_password_headers(device_id: str, device_fp: str, lifecycle_id: str) -> dict[str, str]:
    """``loginByPassword`` 的请求头。

    在通行证通用头基础上改三处, 与抓包 (0104_POST_loginByPassword.json) 对齐:

    * ``referer`` —— 跨源 XHR 只发 origin ``https://user.mihoyo.com/`` (抓包实测),
      完整登录页 URL 放在 ``x-rpc-mi_referrer`` 里, 路由为 ``#/login/password``;
    * ``x-rpc-sdk_version`` —— 登录页自报的 ``2.57.0`` (扫码链路用的是 2.53.1);
    * ``x-rpc-source: v2.webLogin`` —— 登录页 JS (模块 8612) 对密码登录强制注入。

    注意 ``device_fp`` 由调用方决定: 抓包里登录页**没有**跑设备指纹 SDK,
    ``x-rpc-device_fp`` 是空串, 因此 :meth:`Authenticator.login_password`
    也传空; DEVICEFP 只保留在 cookie 里供后续调度复用。
    """
    headers = make_passport_headers(device_id, device_fp, lifecycle_id)
    headers.update(
        {
            "referer": LOGIN_ORIGIN,
            "x-rpc-mi_referrer": PASSWORD_REFERER,
            "x-rpc-sdk_version": LOGIN_PLATFORM_SDK_VERSION,
            "x-rpc-source": X_RPC_SOURCE_WEB_LOGIN,
        }
    )
    return headers


# ---------------------------------------------------------------------------
# 密码登录的 RSA 加密 (与登录页 JSEncrypt 行为一致)
# ---------------------------------------------------------------------------
_PUBLIC_KEY_CACHE: dict[str, Any] = {}


def load_rsa_public_key(key_b64: str = RSA_PUBLIC_KEY_B64) -> Any:
    """把 DER SubjectPublicKeyInfo 的 base64 解析成 RSA 公钥对象 (带缓存)。

    参数:
        key_b64: 无 PEM 装甲的 base64 公钥; 默认 :data:`RSA_PUBLIC_KEY_B64`。

    返回:
        ``cryptography`` 的 ``RSAPublicKey``。``cryptography`` 是延迟导入的,
        仅探活 / 调度链路不会为它付出 import 代价。
    """
    cached = _PUBLIC_KEY_CACHE.get(key_b64)
    if cached is None:
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        try:
            cached = load_der_public_key(base64.b64decode(key_b64))
        except (ValueError, TypeError) as exc:  # 非法 base64 / 非 SPKI DER
            raise ValueError(f"RSA 公钥解析失败: {exc}") from exc
        _PUBLIC_KEY_CACHE[key_b64] = cached
    return cached


def rsa_encrypt_b64(text: str, *, key_b64: str = RSA_PUBLIC_KEY_B64) -> str:
    """按登录页行为加密单个字段: RSA PKCS#1 v1.5 + base64。

    与 JSEncrypt 3.2.1 的 ``encrypt()`` 等价 (``pkcs1pad2`` 随机填充 →
    ``doPublic`` → ``hex2b64``)。1024 位密钥单块上限 117 字节, 对账号 /
    密码足够; 超长会抛 :class:`ValueError` 而不是静默截断。

    参数:
        text: 明文 (账号、密码、手机号、区号、身份证号等)。
        key_b64: 覆盖公钥, 便于测试注入自签密钥。

    返回:
        128 字节密文的 base64 (172 字符, 每次调用因随机填充而不同)。
    """
    if not text:
        raise ValueError("待加密字段不能为空")
    from cryptography.hazmat.primitives.asymmetric import padding

    key = load_rsa_public_key(key_b64)
    try:
        ciphertext = key.encrypt(text.encode("utf-8"), padding.PKCS1v15())
    except ValueError as exc:
        raise ValueError(
            f"字段无法用 RSA-{key.key_size} 加密 (单块上限 {key.key_size // 8 - 11} 字节): {exc}"
        ) from exc
    return base64.b64encode(ciphertext).decode("ascii")


def validate_rsa_ciphertext(text: str, *, key_b64: str = RSA_PUBLIC_KEY_B64) -> str:
    """Validate a saved RSA block without decrypting or exposing its contents."""
    size = load_rsa_public_key(key_b64).key_size // 8
    if not isinstance(text, str) or len(text) != 4 * ((size + 2) // 3):
        raise ValueError("Invalid RSA ciphertext length")
    try:
        block = base64.b64decode(text, validate=True)
    except ValueError:
        raise ValueError("Invalid RSA ciphertext encoding") from None
    if len(block) != size or base64.b64encode(block).decode("ascii") != text:
        raise ValueError("Invalid RSA ciphertext block")
    return text


def build_password_login_body(
    account: str,
    password: str,
    *,
    encrypted: bool = False,
    key_b64: str = RSA_PUBLIC_KEY_B64,
) -> dict[str, str]:
    """构造 ``loginByPassword`` 请求体 —— 只有两个 RSA 密文字段。

    抓包中的请求体为 ``{"account": "...", "password": "..."}``,
    ``Content-Length: 372`` = 两个 172 字符密文 + 28 字节 JSON 骨架。
    """
    if encrypted:
        return {
            "account": validate_rsa_ciphertext(account, key_b64=key_b64),
            "password": validate_rsa_ciphertext(password, key_b64=key_b64),
        }
    return {
        "account": rsa_encrypt_b64(account, key_b64=key_b64),
        "password": rsa_encrypt_b64(password, key_b64=key_b64),
    }


# ---------------------------------------------------------------------------
# 极验 (aigis) 风控挑战
# ---------------------------------------------------------------------------
def parse_aigis_header(value: str | None) -> dict[str, Any] | None:
    """解析响应头 ``x-rpc-aigis``。

    登录页 JS 的处理是 ``JSON.parse(header)`` → ``{session_id, data}``, 其中
    ``data`` 又是**一段 JSON 字符串** (极验 init 参数)。这里同样把内层解出来,
    解析失败返回 ``None`` 而不是抛异常 —— 该头只在风控挑战时出现。

    参数:
        value: 原始响应头值; ``None`` / 空串返回 ``None``。

    返回:
        ``{"session_id": str, "data": dict | str, "raw": dict}`` 或 ``None``。
    """
    if not value:
        return None
    try:
        outer = json.loads(value)
    except ValueError:
        return None
    if not isinstance(outer, dict):
        return None
    data = outer.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            pass
    return {
        "session_id": str(outer.get("session_id") or ""),
        "data": data,
        "raw": outer,
    }


def build_aigis_header(session_id: str, result: Mapping[str, Any]) -> str:
    """把人工解出的极验结果拼成重试用的 ``x-rpc-aigis`` 头。

    与登录页 JS 的 ```${session_id};${btoa(JSON.stringify(gt_result))}``` 等价:
    ``session_id`` 来自 :func:`parse_aigis_header`, ``result`` 是极验回调的
    ``{geetest_challenge, geetest_validate, geetest_seccode, ...}``。

    参数:
        session_id: ``x-rpc-aigis`` 里的 ``session_id``。
        result: 极验 SDK 返回的结果字典。

    返回:
        ``"<session_id>;<base64(紧凑 JSON)>"`` 形式的响应头值。
    """
    if not session_id:
        raise ValueError("session_id 不能为空")
    payload = json.dumps(dict(result), separators=(",", ":"), ensure_ascii=False)
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{session_id};{encoded}"


class PasswordLoginError(RuntimeError):
    """密码登录失败 (非零 retcode 或网络层错误)。

    属性:
        retcode: 通行证 retcode; 网络层错误时沿用失败路径约定的 ``-2``。
        message: 服务端 message。
        hint: 按 retcode 给出的处置建议 (:data:`PASSWORD_LOGIN_HINTS`), 可为空。
        aigis: 风控挑战解析结果, 见 :func:`parse_aigis_header`; 非 -3101 时为 None。
        verify: 身份验证挑战 (``-3235`` 时的 ``X-Rpc-Verify`` 解析结果,
            见 ``core.verifier.RiskChallenge``); 其它情况为 None。
        payload: 原始响应信封, 便于调用方自行判断。
    """

    def __init__(
        self,
        message: str,
        *,
        retcode: int | None = None,
        hint: str = "",
        aigis: dict[str, Any] | None = None,
        verify: Any = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.retcode = retcode
        self.hint = hint
        self.aigis = aigis
        self.verify = verify
        self.payload = payload or {}


class QRLoginError(RuntimeError):
    """Safe structured QR terminal status; never includes a ticket or cookie."""

    def __init__(self, status, retcode=None):
        self.status = status
        self.retcode = retcode if type(retcode) is int else None
        super().__init__("QR login ended (%s)." % status)



# ---------------------------------------------------------------------------
# Authenticator
# ---------------------------------------------------------------------------
class Authenticator:
    """米哈游通行证认证客户端: 扫码登录 / 账号密码登录 + cookie 有效性校验。

    本类只承担**纯认证逻辑**: 不读、不写凭据文件 —— 文件 IO 由调用方
    (``core.cloud_game.CloudGame`` 或 ``qrcode_login.py`` 这样的 CLI) 通过
    模块级 :func:`load_cookie` / :func:`save_cookie` 自行完成。

    与 :class:`core.dispatcher.Dispatcher` 解耦 —— Dispatcher 永远不会 import
    本类, 只共享本模块顶部的常量与纯工具函数。

    典型用法::

        auth = Authenticator()
        valid, info = auth.check(cookie)               # 探活
        if not valid:
            new_cookie = auth.login_qrcode(            # 扫码并返回 cookie
                existing_cookie=cookie,                # 复用设备指纹
            )
        # 或者: 账号密码登录 (同一份 cookie 形态, 但可能触发极验风控)
        new_cookie = auth.login_password(
            "you@example.com", "password",
            existing_cookie=cookie,
        )

    GUI / CLI 集成时通过 ``on_status`` 回调接管文字进度提示,
    通过 ``terminal=False`` / ``save_png=False`` 控制二维码渲染目的地。
    """

    def __init__(
        self,
        *,
        qr_dir: Path | str = "log",
        logger: logging.Logger | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """初始化认证客户端。

        参数:
            qr_dir: 扫码时二维码 PNG 的输出目录, 不存在会自动创建。
            logger: 自定义日志器; 默认使用 ``core.log.get_logger("auth")``。
        """
        self.qr_dir = Path(qr_dir)
        self.logger = logger or get_logger("auth")
        self.headers = dict(headers or {})

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def check(self, cookie: str | None, *, timeout: float = 15.0) -> tuple[bool, dict[str, Any]]:
        """通过 ``webVerifyForGame`` 拉取个人信息, 判断 cookie 是否仍然有效。

        参数:
            cookie: 单行 Cookie header; ``None`` 视为缺失, 直接返回失败。
            timeout: 单次请求超时秒数。

        返回:
            ``(valid, info)``。``info`` 至少包含 ``retcode``、``message``、
            ``cookies`` (解析后的 dict) 与 ``missing`` (缺失的必需字段列表);
            ``valid=True`` 时还会带 ``aid`` / ``mid`` / ``mobile`` / ``realname`` /
            ``is_adult`` / ``user_info``; ``valid=False`` 且发生网络错误时
            带 ``error``。

        失败路径:
            * cookie=None —— 不发请求, ``retcode=-1``, ``message="missing cookie"``。
            * 必需字段缺失/占位 —— 不发请求, ``retcode=-1``。
            * 网络异常 —— ``retcode=-2``, ``error`` 为异常消息。
            * 服务端拒绝 —— ``retcode`` 为服务端 retcode (例如 ``-100`` 表示
              token 失效)。
        """
        if cookie is None:
            return False, {
                "retcode": -1,
                "message": "missing cookie",
                "error": "missing cookie",
                "cookies": {},
                "missing": list(REQUIRED_COOKIES),
            }
        cookies = parse_cookie_header(cookie)
        info: dict[str, Any] = {"cookies": cookies}

        missing = [
            name for name in REQUIRED_COOKIES
            if not cookies.get(name) or is_placeholder(cookies[name])
        ]
        info["missing"] = missing
        if missing:
            info["retcode"] = -1
            info["message"] = f"缺少必需 Cookie 字段: {missing}"
            info["error"] = info["message"]
            return False, info

        device_id = cookies.get("_MHYUUID") or str(uuid.uuid4())
        device_fp = cookies.get("DEVICEFP") or gen_device_fp()
        lifecycle_id = cookies.get("MIHOYO_LOGIN_PLATFORM_LIFECYCLE_ID") or gen_lifecycle_id()

        headers = make_verify_headers(make_passport_headers(device_id, device_fp, lifecycle_id))
        headers.update(self.headers)
        headers["cookie"] = cookie

        try:
            response = requests.post(WEB_VERIFY_URL, headers=headers, json={}, timeout=timeout)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            info["retcode"] = -2
            info["message"] = f"请求失败: {exc}"
            info["error"] = str(exc)
            return False, info
        if response.status_code >= 300 or not isinstance(payload, dict):
            info["retcode"] = -2
            info["message"] = "Authentication HTTP or response error"
            info["error"] = info["message"]
            return False, info


        info["retcode"] = payload.get("retcode")
        info["message"] = payload.get("message")
        if payload.get("retcode") != 0:
            return False, info

        user_info = (payload.get("data") or {}).get("user_info") or {}
        info["user_info"] = user_info
        info["aid"] = user_info.get("aid", "")
        info["mid"] = user_info.get("mid", "")
        info["mobile"] = user_info.get("mobile", "")
        info["realname"] = user_info.get("realname", "")
        info["is_adult"] = user_info.get("is_adult", 0)
        return True, info

    # ------------------------------------------------------------------
    # 扫码登录
    # ------------------------------------------------------------------
    def login_qrcode(
        self,
        *,
        existing_cookie: str | None = None,
        poll_interval: float = 3.0,
        timeout: int = 180,
        verify: bool = True,
        save_png: bool = True,
        terminal: bool = True,
        light_terminal: bool = False,
        on_status: Callable[[str], None] | None = None,
        on_qr: Callable[[bytes], None] | None = None,
        on_qr_status: Callable[[str], None] | None = None,
        cancel_event: Event | None = None,
    ) -> str:
        """完整扫码登录流程, 返回单行 cookie header (不写盘)。

        步骤::

            createQRLogin → 渲染二维码 → 轮询 queryQRLoginStatus → (可选) webVerifyForGame

        参数:
            existing_cookie: 已有 cookie (单行 header); 用于复用 ``_MHYUUID`` /
                ``DEVICEFP`` 等设备指纹字段, ``None`` 时全部新生成。文件读取由
                调用方完成, 本方法不碰文件系统。
            poll_interval: 轮询间隔秒, 过短会被服务端判为失效。
            timeout: 等待扫码总超时秒。
            verify: 是否在 ``Confirmed`` 后追加一次 ``webVerifyForGame``,
                与浏览器实际行为一致并刷新 cookie。
            save_png: 是否在 :attr:`qr_dir` 下保存 PNG 二维码。
            terminal: 是否把二维码渲染到终端 (适合 SSH 场景)。
            light_terminal: 浅色终端时取消反色, 默认按深色终端反色显示。
            on_status: 进度回调, 每个里程碑用一行字符串报告; ``None`` 时
                通过 :attr:`logger` 以 INFO 级别输出。
            on_qr: receives in-memory PNG bytes; no ticket or URL is logged.
            on_qr_status: structured official QR status (Created/Scanned/Confirmed).
            cancel_event: interrupts polling waits; an in-flight HTTP request remains
                bounded by its request timeout. Use save_png=False for WebUI login.

        返回:
            服务端确认后的单行 ``Cookie`` header (含必需字段)。调用方负责
            后续持久化 (例如通过模块级 :func:`save_cookie`)。

        异常:
            ``RuntimeError``: 二维码失效、轮询超时、服务端非零 retcode 等。
        """
        # qrcode 仅扫码流程需要, 延迟导入避免 dispatcher 链路被动加载 PIL。
        import qrcode

        def report(message: str) -> None:
            if on_status is not None:
                on_status(message)
            else:
                self.logger.info(message)

        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("QR login cancelled")

        check_cancelled()

        bootstrap = self._build_bootstrap(existing_cookie)
        device_id = bootstrap["_MHYUUID"]
        device_fp = bootstrap["DEVICEFP"]
        lifecycle_id = bootstrap["MIHOYO_LOGIN_PLATFORM_LIFECYCLE_ID"]

        session = requests.Session()
        for name, value in bootstrap.items():
            session.cookies.set(name, value, domain=".mihoyo.com", path="/")
        headers = make_passport_headers(device_id, device_fp, lifecycle_id)
        headers.update(self.headers)

        # ---- 1) 申请二维码 ----
        report(
            f"[1/4] device_id={device_id}, device_fp={device_fp}, lifecycle_id={lifecycle_id}"
        )
        report("       POST createQRLogin ...")
        qr_resp = self._post_json(session, CREATE_QR_URL, headers, {})
        if qr_resp.get("retcode") != 0:
            raise QRLoginError("Failed", qr_resp.get("retcode"))
        qr_url = qr_resp["data"]["url"]
        ticket = qr_resp["data"]["ticket"]

        # ---- 2) 渲染二维码 ----
        check_cancelled()
        qr = qrcode.QRCode(border=2, box_size=10)
        qr.add_data(qr_url)
        qr.make(fit=True)
        if on_qr is not None:
            buffer = io.BytesIO()
            qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
            on_qr(buffer.getvalue())
        if save_png:
            self.qr_dir.mkdir(parents=True, exist_ok=True)
            qr_path = self.qr_dir / ("qrcode_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
            qr.make_image(fill_color="black", back_color="white").save(qr_path)
            report(f"[2/4] 二维码已保存: {qr_path.resolve()}")
        else:
            report("[2/4] 二维码 (跳过 PNG)")
        if terminal:
            # 深色终端 invert=True 用 █, 浅色终端 light_terminal=True 翻回正色;
            # tty=False 用半块字符 (▀▄), 体积是整块的一半, 多数手机仍可扫。
            print()
            qr.print_ascii(tty=False, invert=not light_terminal)
            print()
        report("       请用「米游社」或 「云·星穹铁道」移动端（推荐）扫码 → 在手机上确认登录")

        # ---- 3) 轮询登录状态 ----
        report(f"[3/4] 开始轮询 (间隔 {poll_interval}s, 超时 {timeout}s) ...")
        user_info = self._poll_status(
            session, headers, ticket,
            poll_interval=poll_interval,
            timeout=timeout,
            report=report,
            on_qr_status=on_qr_status,
            cancel_event=cancel_event,
        )
        check_cancelled()
        report("       QR login confirmed")

        # ---- 4) 可选: webVerifyForGame ----
        if verify:
            report("[4/4] POST webVerifyForGame ...")
            verify_resp = self._post_json(session, WEB_VERIFY_URL, make_verify_headers(headers), {})
            if verify_resp.get("retcode") != 0:
                report(
                    f"       警告: webVerifyForGame retcode={verify_resp.get('retcode')}"
                    f" message={verify_resp.get('message')}"
                )
            else:
                report("       OK")
        else:
            report("[4/4] 跳过 webVerifyForGame (verify=False)")

        # ---- 整理 cookies ----
        return self._cookie_header(session, bootstrap)

    # ------------------------------------------------------------------
    # 密码登录
    # ------------------------------------------------------------------
    def login_password(
        self,
        account: str,
        password: str,
        *,
        encrypted: bool = False,
        existing_cookie: str | None = None,
        verify: bool = True,
        extra_headers: Mapping[str, str] | None = None,
        on_aigis: Callable[[dict[str, Any]], str] | None = None,
        on_sms_code: Callable[[dict[str, Any]], str] | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        """账号密码登录, 返回单行 cookie header (不写盘)。

        步骤::

            loginByPassword (account/password 各自 RSA 加密) → (可选) webVerifyForGame

        与 :meth:`login_qrcode` 的差异仅在于凭据的获取方式: 密码登录是**一次
        HTTP 请求**, 没有轮询; 两者返回的 cookie 形态、后续 ``check()`` /
        ``Dispatcher`` 的用法完全一致。

        参数:
            account: 通行证账号 (邮箱 / 手机号), 明文; 内部 RSA 加密后发送。
            password: 明文密码; 内部 RSA 加密后发送, 不会写日志。
            encrypted: True accepts saved RSA base64 blocks, validates them, and
                reuses the same request flow without encrypting them again.
            existing_cookie: 已有 cookie (单行 header); 用于复用 ``_MHYUUID`` /
                ``DEVICEFP`` 等设备指纹字段, ``None`` 时全部新生成。
            verify: 登录成功后是否追加一次 ``webVerifyForGame`` 刷新 cookie,
                与浏览器行为一致。
            extra_headers: 追加/覆盖请求头。风控挑战 (retcode ``-3101``) 时可用它带
                上人工解出的 ``{"x-rpc-aigis": build_aigis_header(...)}``。
            on_aigis: 人工解验证码的回调。收到 ``-3101`` 且本次请求未携带
                ``x-rpc-aigis`` 时被调用, 入参是 :func:`parse_aigis_header` 的结果
                (``{"session_id", "data", "raw"}``), 返回值直接作为 ``x-rpc-aigis``
                请求头, 然后用**同一个请求体**重试一次 (与浏览器 axios 重试一致)。
                现成的实现见 ``core.aigis.browser_aigis_solver()``。
            on_sms_code: 身份验证 (``-3235 AccountRisky``) 时收短信验证码的回调。
                入参形如 ``{"mobile": 脱敏手机号, "methods": [1], "info": {...}}``,
                返回用户输入的验证码。给了它, 库会自动跑完
                ``markRiskAction → getActionTicketInfo → 发短信(含极验)
                → verifyActionTicketPartly → checkRiskVerified``,
                并由 ``checkRiskVerified`` 直接下发登录态 cookie
                (流程与端点见 :mod:`core.verifier`, 抓包实测无需二次 loginByPassword)。
                不传则 ``-3235`` 直接抛 :class:`PasswordLoginError` (``err.verify`` 带挑战详情)。
            session: 复用外部 ``requests.Session`` (沿用其 cookie jar);
                ``None`` 时内部新建。传入的 session 不会被关闭。
            timeout: 单次请求超时秒数。
            on_status: 进度回调, 每个里程碑一行字符串; ``None`` 时走
                :attr:`logger`。

        返回:
            登录后的单行 ``Cookie`` header (含 ``cookie_token_v2`` /
            ``account_mid_v2`` 等必需字段)。调用方负责持久化 (见
            :func:`save_cookie`)。

        异常:
            ``ValueError``: account / password 为空。
            :class:`PasswordLoginError`: 非零 retcode (含风控 ``-3101``)、网络异常
                或 HTTP 层错误。风控未解决时 ``err.aigis`` 携带极验会话, 见
                :func:`parse_aigis_header` / :func:`build_aigis_header`。
        """
        if not account:
            raise ValueError("account 不能为空")
        if not password:
            raise ValueError("password 不能为空")
        body = build_password_login_body(account, password, encrypted=encrypted)

        def report(message: str) -> None:
            if on_status is not None:
                on_status(message)
            else:
                self.logger.info(message)

        bootstrap = self._build_bootstrap(existing_cookie)
        device_id = bootstrap["_MHYUUID"]
        lifecycle_id = bootstrap["MIHOYO_LOGIN_PLATFORM_LIFECYCLE_ID"]
        # DEVICEFP 不参与请求头 (抓包里 x-rpc-device_fp 为空), 只留在 cookie 里。

        if session is None:
            session = requests.Session()
        for name, value in bootstrap.items():
            session.cookies.set(name, value, domain=".mihoyo.com", path="/")

        headers = make_password_headers(device_id, "", lifecycle_id)
        headers.update(self.headers)
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})

        report(f"[1/3] device_id={device_id}, lifecycle_id={lifecycle_id}")
        report("       POST loginByPassword (account/password 已 RSA 加密) ...")

        payload, response = self._post_password_login(session, headers, body, timeout=timeout)

        # ---- 风控: -3101 需要极验, 交给 on_aigis 人工解一次后重试 ----
        if (
            payload.get("retcode") == RETCODE_NEED_AIGIS
            and on_aigis is not None
            and not headers.get("x-rpc-aigis")
        ):
            challenge = parse_aigis_header(response.headers.get("x-rpc-aigis"))
            report("[风控] retcode=-3101: 需要极验验证码")
            try:
                header_value = str(on_aigis(challenge or {}))
            except Exception as exc:
                raise PasswordLoginError(
                    f"极验验证未完成: {exc}",
                    retcode=RETCODE_NEED_AIGIS,
                    hint=PASSWORD_LOGIN_HINTS.get(RETCODE_NEED_AIGIS, ""),
                    aigis=challenge,
                    payload=payload,
                ) from exc
            if not header_value:
                raise PasswordLoginError(
                    "on_aigis 没有返回 x-rpc-aigis 头值",
                    retcode=RETCODE_NEED_AIGIS,
                    hint=PASSWORD_LOGIN_HINTS.get(RETCODE_NEED_AIGIS, ""),
                    aigis=challenge,
                    payload=payload,
                )
            headers["x-rpc-aigis"] = header_value
            # 浏览器 axios 重试时请求体不变 (密文只算一次), 这里同样复用 body。
            report("       已带上 x-rpc-aigis, 重试 loginByPassword ...")
            payload, response = self._post_password_login(session, headers, body, timeout=timeout)

        retcode = payload.get("retcode")

        # ---- 风控: -3235 账号级风险, 需要身份验证 (手机短信/极验) ----
        verified_data: dict[str, Any] | None = None
        if retcode == RETCODE_ACCOUNT_RISKY:
            from .verifier import (
                RiskVerificationError, VERIFY_TYPE_GEETEST,
                complete_risk_verification, parse_verify_header,
            )

            challenge = parse_verify_header(response.headers.get("x-rpc-verify"))
            if challenge is None or not (challenge.risk_ticket or challenge.action_ticket):
                raise PasswordLoginError(
                    f"loginByPassword 失败: retcode={retcode} message={payload.get('message')}"
                    " —— 账号命中风控且响应缺少 X-Rpc-Verify 头, 请到网页端处理",
                    retcode=retcode,
                    hint=PASSWORD_LOGIN_HINTS.get(retcode, ""),
                    payload=payload,
                )
            if on_sms_code is None and challenge.verify_type != VERIFY_TYPE_GEETEST:
                raise PasswordLoginError(
                    f"loginByPassword 失败: retcode={retcode} message={payload.get('message')}"
                    f" —— 需要身份验证 (verify_type={challenge.verify_type_name});"
                    " 传 on_sms_code=<回调> 让库自动走完验证, 或到网页端验证",
                    retcode=retcode,
                    hint=PASSWORD_LOGIN_HINTS.get(retcode, ""),
                    verify=challenge,
                    payload=payload,
                )
            report(
                f"[风控] retcode=-3235 账号级风险, 需要身份验证"
                f" (verify_type={challenge.verify_type_name})"
            )
            try:
                verified_data = complete_risk_verification(
                    session,
                    headers,
                    challenge,
                    on_sms_code=on_sms_code,
                    on_aigis=on_aigis,
                    on_status=on_status,
                    timeout=timeout,
                )
            except RiskVerificationError as exc:
                raise PasswordLoginError(
                    f"身份验证失败: {exc}",
                    retcode=exc.retcode if exc.retcode is not None else retcode,
                    hint=f"步骤 {exc.stage}" if exc.stage else "",
                    verify=challenge,
                    aigis=exc.aigis,
                    payload=exc.payload,
                ) from exc

        if retcode != 0 and verified_data is None:
            aigis = parse_aigis_header(response.headers.get("x-rpc-aigis"))
            hint = PASSWORD_LOGIN_HINTS.get(retcode, "")
            detail = f"loginByPassword 失败: retcode={retcode} message={payload.get('message')}"
            if hint:
                detail += f" —— {hint}"
            raise PasswordLoginError(
                detail,
                retcode=retcode,
                hint=hint,
                aigis=aigis,
                payload=payload,
            )

        # 身份验证成功时登录态由 checkRiskVerified 下发, 不再解析 loginByPassword 的 data
        data = verified_data if verified_data is not None else (payload.get("data") or {})
        user_info = data.get("user_info") or {}
        aid = user_info.get("aid", "")
        mid = user_info.get("mid", "")
        safe_mobile = user_info.get("mobile", "")
        report(f"       登录成功: aid={aid}, mid={mid}, mobile={safe_mobile}")

        realname_info = data.get("realname_info") or {}
        if data.get("need_realperson") or realname_info.get("required"):
            report(
                "       注意: 服务端要求实名认证"
                f" (action_type={realname_info.get('action_type') or '-'})"
                ", 网页端如需补实名请先到通行证页面完成"
            )
        reactivate = data.get("reactivate_info") or {}
        if reactivate.get("required"):
            report("       注意: 账号处于注销冷静期, 需要先恢复账号 (reactivateAccount)")

        if verify:
            report("[2/3] POST webVerifyForGame ...")
            verify_resp = self._post_json(session, WEB_VERIFY_URL, make_verify_headers(headers), {})
            if verify_resp.get("retcode") != 0:
                report(
                    f"       警告: webVerifyForGame retcode={verify_resp.get('retcode')}"
                    f" message={verify_resp.get('message')}"
                )
            else:
                report("       OK")
        else:
            report("[2/3] 跳过 webVerifyForGame (verify=False)")

        report("[3/3] 整理 Cookie ...")
        return self._cookie_header(session, bootstrap)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _cookie_header(session: requests.Session, bootstrap: dict[str, str]) -> str:
        """把 session 里的 cookie 拼成单行 header, 并补齐 bootstrap 字段。

        兜底逻辑: bootstrap 写入的 ``_MHYUUID`` 等可能没被服务端再次
        ``Set-Cookie``, 必须保留, 否则 ``Dispatcher`` 拿不到设备 ID。
        """
        cookies: dict[str, str] = {c.name: c.value for c in session.cookies}
        for name, value in bootstrap.items():
            cookies.setdefault(name, value)
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    @staticmethod
    def _post_password_login(
        session: requests.Session,
        headers: dict[str, str],
        body: dict[str, str],
        *,
        timeout: float,
    ) -> tuple[dict[str, Any], requests.Response]:
        """POST ``loginByPassword``, 把传输层异常统一成 :class:`PasswordLoginError`。

        返回 ``(响应信封, 原始响应)`` —— 风控时 ``x-rpc-aigis`` 只能从响应对象读。
        """
        try:
            return Authenticator._post_json_full(
                session, PASSWORD_LOGIN_URL, headers, body, timeout=timeout
            )
        except requests.RequestException as exc:
            raise PasswordLoginError(f"loginByPassword 请求失败: {exc}", retcode=-2) from exc
        except RuntimeError as exc:  # 非 JSON / HTTP >= 300, 由 _post_json_full 抛出
            raise PasswordLoginError(f"loginByPassword 请求失败: {exc}", retcode=-2) from exc

    def _build_bootstrap(self, existing_cookie: str | None) -> dict[str, str]:
        """从 ``existing_cookie`` 复用设备指纹相关字段, 缺失/失效时新生成。"""
        bootstrap: dict[str, str] = {}
        existing = parse_cookie_header(existing_cookie or "")
        for key in ("_MHYUUID", "DEVICEFP_SEED_ID", "DEVICEFP_SEED_TIME", "DEVICEFP"):
            value = existing.get(key)
            if value and not is_placeholder(value):
                bootstrap[key] = value

        bootstrap.setdefault("_MHYUUID", str(uuid.uuid4()))
        bootstrap.setdefault("DEVICEFP_SEED_ID", secrets.token_hex(8))
        bootstrap.setdefault("DEVICEFP_SEED_TIME", str(int(time.time() * 1000)))
        bootstrap.setdefault("DEVICEFP", gen_device_fp())
        bootstrap["mi18nLang"] = "zh-cn"
        bootstrap["MIHOYO_LOGIN_PLATFORM_LIFECYCLE_ID"] = gen_lifecycle_id()
        return bootstrap

    @staticmethod
    def _post_json(
        session: requests.Session,
        url: str,
        headers: dict[str, str],
        body: dict,
    ) -> dict[str, Any]:
        """带详细错误信息的 JSON POST, 只返回响应信封。"""
        payload, _ = Authenticator._post_json_full(session, url, headers, body)
        return payload

    @staticmethod
    def _post_json_full(
        session: requests.Session,
        url: str,
        headers: dict[str, str],
        body: dict,
        *,
        timeout: float = 20.0,
    ) -> tuple[dict[str, Any], requests.Response]:
        """JSON POST, 同时返回响应信封与原始响应 (风控头只有响应对象里有)。

        请求体用**紧凑 JSON** 序列化, 与浏览器 axios 的行为一致: 抓包里
        ``loginByPassword`` 的 ``Content-Length: 372`` 正是 ``{"account":"…",
        "password":"…"}`` 紧凑形式; ``requests`` 默认的 ``json=`` 会插入空格,
        变成 374。两者服务端都能解析, 这里按抓包对齐。
        """
        payload_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        response = session.post(url, headers=headers, data=payload_bytes, timeout=timeout)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"{url} 返回非 JSON HTTP {response.status_code}: {response.text[:200]}"
            ) from exc
        if response.status_code >= 300:
            raise RuntimeError(f"{url} HTTP {response.status_code}: {payload}")
        return payload, response

    @classmethod
    def _poll_status(
        cls,
        session: requests.Session,
        headers: dict[str, str],
        ticket: str,
        *,
        poll_interval: float,
        timeout: int,
        report: Callable[[str], None],
        on_qr_status: Callable[[str], None] | None = None,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        """轮询 queryQRLoginStatus 直到 ``Confirmed`` 或终态/超时。"""
        deadline = time.monotonic() + max(timeout, 1)
        last_status: str | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("QR login cancelled")
            if time.monotonic() >= deadline:
                raise QRLoginError("Timeout")
            status_resp = cls._post_json(session, QUERY_QR_URL, headers, {"ticket": ticket})
            if status_resp.get("retcode") != 0:
                raise QRLoginError("Failed", status_resp.get("retcode"))
            data = status_resp["data"]
            status = data.get("status") or ""
            if status != last_status:
                report(f"       status: {status}")
                last_status = status
                if on_qr_status is not None:
                    on_qr_status(status)
            if status == "Confirmed":
                return data.get("user_info") or {}
            if status in TERMINAL_FAIL_STATUSES:
                raise QRLoginError(status)
            if cancel_event is not None:
                if cancel_event.wait(poll_interval):
                    raise RuntimeError("QR login cancelled")
            else:
                time.sleep(poll_interval)


__all__ = [
    "Authenticator",
    "QRLoginError",
    "CLOUD_REFERER",
    "CREATE_QR_URL",
    "GAME_BIZ",
    "LOGIN_ORIGIN",
    "LOGIN_PLATFORM_SDK_VERSION",
    "LOGIN_PLATFORM_URL",
    "PASSPORT_APP_ID",
    "PASSPORT_BASE",
    "PASSPORT_REFERER",
    "PASSPORT_SDK_VERSION",
    "PASSWORD_LOGIN_HINTS",
    "PASSWORD_LOGIN_URL",
    "PASSWORD_REFERER",
    "PasswordLoginError",
    "QUERY_QR_URL",
    "REQUIRED_COOKIES",
    "RETCODE_ACCOUNT_LOCKED",
    "RETCODE_ACCOUNT_PWD_TRY_TOO_MUCH",
    "RETCODE_ACCOUNT_RISKY",
    "RETCODE_ACCOUNT_SAFELY_FORBIDDEN",
    "RETCODE_ACCOUNT_SELF_LOGIN_RESTRICTION",
    "RETCODE_NEED_AIGIS",
    "RSA_PUBLIC_KEY_B64",
    "SEC_CH_UA",
    "SESSION_SDK_VERSION",
    "TERMINAL_FAIL_STATUSES",
    "USER_AGENT",
    "WEB_VERIFY_URL",
    "X_RPC_SOURCE_WEB_LOGIN",
    "build_aigis_header",
    "build_password_login_body",
    "gen_device_fp",
    "gen_lifecycle_id",
    "is_placeholder",
    "load_cookie",
    "load_rsa_public_key",
    "make_passport_headers",
    "make_password_headers",
    "make_verify_headers",
    "parse_aigis_header",
    "parse_cookie_header",
    "rsa_encrypt_b64",
    "validate_rsa_ciphertext",
    "save_cookie",
]
