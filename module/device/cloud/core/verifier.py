"""通行证「身份验证 / 风控挑战」接口 —— ``-3235 AccountRisky`` 的解法。

抓包依据（``rpg-cn-rtcapp-log…_2026_09_30_19_49_14.har``，2026-09-30 19:47~19:48）：

===========  ====================================================================
步骤          接口
===========  ====================================================================
1  登录失败    ``POST /account/ma-cn-passport/web/loginByPassword`` → ``retcode -3235``
             响应头 ``X-Rpc-Verify``：
             ``{"action_ticket":"<RISK_TICKET>","verify_str":"<JSON 字符串>"}``
             ``verify_str`` = ``{"verify_type":2,"ticket":"<ACTION_TICKET>",
             "action_type":"","risk_actions":[{...,"action_type":"verify_for_component"}]}``
2  标记         ``POST /account/ma-cn-passport/passport/markRiskAction``
             ``{"risk_ticket": <RISK_TICKET>, "action_ticket": <ACTION_TICKET>}``
3  取验证信息   ``GET  /account/ma-cn-verifier/verifier/getActionTicketInfo``
             ``?action_ticket=<ACTION_TICKET>&action_type=verify_for_component``
             返回 ``verify_info.{status,verify_method_combinations,chosen_methods}`` 与脱敏 ``user_info``
4  发短信       ``POST /account/ma-cn-verifier/verifier/createMobileCaptchaByActionTicket``
             ``{"action_ticket": <ACTION_TICKET>, "action_type": "verify_for_component"}``
             首次返回 ``-3101`` + 响应头 ``X-Rpc-Aigis``（``risk_type: "icon"`` 的极验点选），
             解出验证码后带 ``x-rpc-aigis`` 重发同一 body → ``retcode 0``（短信已下发）
5  校验码       ``POST /account/ma-cn-verifier/verifier/verifyActionTicketPartly``
             ``{"action_ticket": <ACTION_TICKET>, "action_type": "verify_for_component",
                "verify_method": 1, "mobile_captcha": "<6 位短信码>"}``
6  确认         ``POST /account/ma-cn-passport/web/checkRiskVerified``
             ``{"action_ticket": <RISK_TICKET>}``  ← 注意用的是响应头里的 action_ticket
             → ``retcode 0`` 且 **Set-Cookie 下发登录态**（ltoken_v2 / cookie_token_v2 /
               account_mid_v2 / ltuid_v2 / uni_web_token …）→ 登录完成，无需二次 loginByPassword
===========  ====================================================================

前端 JS 依据（``login-platform/js/login.35d0ab6d.js`` 模块 32139 / ``security-verification.*.js``）：

* ``verify_type`` 枚举：``GEETEST=1, IDENTITY=2, SOFT_FORBIDDEN=3``；本次抓包是 **2 = IDENTITY**
  （登录页把 ``createRiskyTransaction`` 交给 iframe 里的 ``#/security-verification`` 页完成）。
* ``verify_method`` 枚举（模块 79140）：
  ``MobileCaptcha=1, EmailCaptcha=2, IdentityCard=4, SafeMobileCaptcha=8, RealPerson=16,
  Password=32, SignInLocation=64, OrderId=128, SmsUp=256, SelfLiftInfo=1024``。
* ``verify_type=1`` 的支线走 ``POST /common/aigis/api/checkSmartCaptcha``
  （``{ticket}`` → ``{mmt_type:0|1, data}``；``mmt_type=1`` 时解极验后
  ``{ticket, check_data: base64(JSON(validate))}`` 再提交）—— 该分支本次抓包未覆盖，按 JS 还原。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

from .auth import LOGIN_ORIGIN, PASSPORT_BASE, parse_aigis_header
from .log import get_logger

# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
VERIFIER_BASE = f"{PASSPORT_BASE}/account/ma-cn-verifier/verifier"
GET_ACTION_TICKET_INFO_URL = f"{VERIFIER_BASE}/getActionTicketInfo"
CREATE_MOBILE_CAPTCHA_URL = f"{VERIFIER_BASE}/createMobileCaptchaByActionTicket"
CREATE_EMAIL_CAPTCHA_URL = f"{VERIFIER_BASE}/createEmailCaptchaByActionTicket"
VERIFY_ACTION_TICKET_PARTLY_URL = f"{VERIFIER_BASE}/verifyActionTicketPartly"
MODIFY_VERIFY_METHOD_URL = f"{VERIFIER_BASE}/modifyActionTicketVerifyMethod"
MARK_RISK_ACTION_URL = f"{PASSPORT_BASE}/account/ma-cn-passport/passport/markRiskAction"
CHECK_RISK_VERIFIED_URL = f"{PASSPORT_BASE}/account/ma-cn-passport/web/checkRiskVerified"
CHECK_SMART_CAPTCHA_URL = f"{PASSPORT_BASE}/common/aigis/api/checkSmartCaptcha"

ACTION_TYPE_VERIFY_FOR_COMPONENT = "verify_for_component"

# verify_method 位标志（登录页模块 79140 原样抄录）
VERIFY_METHOD_MOBILE = 1
VERIFY_METHOD_EMAIL = 2
VERIFY_METHOD_NAMES: dict[int, str] = {
    1: "手机短信",
    2: "邮箱",
    4: "身份证号",
    8: "安全手机",
    16: "实名",
    32: "密码",
    64: "登录地点",
    128: "订单号",
    256: "短信上行",
    1024: "自助申诉",
}

# verify_type（登录页模块 32139）
VERIFY_TYPE_GEETEST = 1
VERIFY_TYPE_IDENTITY = 2
VERIFY_TYPE_SOFT_FORBIDDEN = 3
VERIFY_TYPE_NAMES: dict[int, str] = {1: "GEETEST", 2: "IDENTITY", 3: "SOFT_FORBIDDEN"}

# iframe (#/security-verification) 的 referer 前缀；tid 每次会话随机
SECURITY_VERIFICATION_REFERER = (
    "https://user.mihoyo.com/login-platform/index.html"
    "?client_type=25&app_id=c90mr1bwo2rk&theme=rpg&token_type=4&game_biz=hkrpg_cn"
    "&message_origin=https%253A%252F%252Fuser.mihoyo.com"
    "&succ_back_type=message%253Alogin-platform%253Averify-success"
    "&fail_back_type=message%253Alogin-platform%253Averify-fail"
    "&ux_mode=popup&iframe_level=2&extra_trace=1&leave_confirm=1"
)


# ---------------------------------------------------------------------------
# X-Rpc-Verify 解析
# ---------------------------------------------------------------------------
@dataclass
class RiskChallenge:
    """``X-Rpc-Verify`` 响应头解析结果。

    命名陷阱（HAR 三处交叉确认）:
        * ``risk_ticket``   = 响应头里的 ``action_ticket`` → 用于
          ``markRiskAction.risk_ticket`` 与 ``checkRiskVerified.action_ticket``;
        * ``action_ticket`` = ``verify_str.ticket`` → 用于 verifier 系列接口的
          ``action_ticket``（getActionTicketInfo / create*/verifyActionTicketPartly）。
    """

    risk_ticket: str
    action_ticket: str
    verify_type: int = -1
    action_type: str = ""
    risk_actions: list[dict[str, Any]] = field(default_factory=list)
    verify_str: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def verify_type_name(self) -> str:
        return VERIFY_TYPE_NAMES.get(self.verify_type, str(self.verify_type))


def parse_verify_header(value: str | None) -> RiskChallenge | None:
    """解析 ``X-Rpc-Verify`` 响应头；缺失/损坏返回 ``None``。

    与登录页 ``RiskyResponseHeaderKey`` 解析逻辑一致：外层 JSON 里的
    ``verify_str`` 是**一段 JSON 字符串**，需要二次 ``json.loads``。
    """
    if not value:
        return None
    try:
        outer = json.loads(value)
    except ValueError:
        return None
    if not isinstance(outer, dict):
        return None
    verify_str = outer.get("verify_str") or {}
    if isinstance(verify_str, str):
        try:
            verify_str = json.loads(verify_str)
        except ValueError:
            verify_str = {"raw": verify_str}
    if not isinstance(verify_str, dict):
        verify_str = {"raw": verify_str}
    risk_actions = verify_str.get("risk_actions") or []
    if not isinstance(risk_actions, list):
        risk_actions = []
    try:
        verify_type = int(verify_str.get("verify_type", -1))
    except (TypeError, ValueError):
        verify_type = -1
    return RiskChallenge(
        risk_ticket=str(outer.get("action_ticket") or ""),
        action_ticket=str(verify_str.get("ticket") or ""),
        verify_type=verify_type,
        action_type=str(verify_str.get("action_type") or ""),
        risk_actions=risk_actions,
        verify_str=verify_str,
        raw=outer,
    )


def security_verification_referer(tid: str, verify_method: int | None = None) -> str:
    """iframe 验证页的 URL：``#/security-verification[/<method>]?tid=<tid>``。"""
    suffix = f"/{verify_method}" if verify_method else ""
    return f"{SECURITY_VERIFICATION_REFERER}#/security-verification{suffix}?tid={tid}"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class RiskVerificationError(RuntimeError):
    """身份验证流程失败。

    属性:
        stage: 失败的步骤名（``markRiskAction`` / ``getActionTicketInfo`` / ...）。
        retcode: 该步骤的服务端 retcode（网络层错误为 ``-2``）。
        aigis: 需要极验挑战时的解析结果（见 :func:`core.auth.parse_aigis_header`）。
        payload: 该步骤的原始响应信封。
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str = "",
        retcode: int | None = None,
        aigis: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.retcode = retcode
        self.aigis = aigis
        self.payload = payload or {}


# ---------------------------------------------------------------------------
# HTTP 辅助（与 auth 保持同样的紧凑 JSON / 错误语义）
# ---------------------------------------------------------------------------
def _post_json(
    session: requests.Session,
    url: str,
    headers: Mapping[str, str],
    body: dict[str, Any],
    *,
    stage: str,
    timeout: float,
) -> tuple[dict[str, Any], requests.Response]:
    payload_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    try:
        response = session.post(url, headers=dict(headers), data=payload_bytes, timeout=timeout)
        payload = response.json()
    except requests.RequestException as exc:
        raise RiskVerificationError(f"{stage} 请求失败: {exc}", stage=stage, retcode=-2) from exc
    except ValueError as exc:
        raise RiskVerificationError(f"{stage} 返回非 JSON: {exc}", stage=stage, retcode=-2) from exc
    if response.status_code >= 300:
        raise RiskVerificationError(
            f"{stage} HTTP {response.status_code}: {payload}", stage=stage, payload=payload
        )
    return payload, response


def _get_json(
    session: requests.Session,
    url: str,
    headers: Mapping[str, str],
    params: dict[str, Any],
    *,
    stage: str,
    timeout: float,
) -> tuple[dict[str, Any], requests.Response]:
    try:
        response = session.get(url, headers=dict(headers), params=params, timeout=timeout)
        payload = response.json()
    except requests.RequestException as exc:
        raise RiskVerificationError(f"{stage} 请求失败: {exc}", stage=stage, retcode=-2) from exc
    except ValueError as exc:
        raise RiskVerificationError(f"{stage} 返回非 JSON: {exc}", stage=stage, retcode=-2) from exc
    if response.status_code >= 300:
        raise RiskVerificationError(
            f"{stage} HTTP {response.status_code}: {payload}", stage=stage, payload=payload
        )
    return payload, response


# ---------------------------------------------------------------------------
# 单步接口
# ---------------------------------------------------------------------------
def mark_risk_action(
    session: requests.Session,
    headers: Mapping[str, str],
    challenge: RiskChallenge,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """``markRiskAction``：告知服务端开始处理这次风控（HAR 里是第一步）。"""
    payload, _ = _post_json(
        session,
        MARK_RISK_ACTION_URL,
        headers,
        {"risk_ticket": challenge.risk_ticket, "action_ticket": challenge.action_ticket},
        stage="markRiskAction",
        timeout=timeout,
    )
    return payload


def get_action_ticket_info(
    session: requests.Session,
    headers: Mapping[str, str],
    challenge: RiskChallenge,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """``getActionTicketInfo``：拿到可选验证方式与脱敏账号信息。"""
    payload, _ = _get_json(
        session,
        GET_ACTION_TICKET_INFO_URL,
        headers,
        {
            "action_ticket": challenge.action_ticket,
            "action_type": challenge.action_type or ACTION_TYPE_VERIFY_FOR_COMPONENT,
        },
        stage="getActionTicketInfo",
        timeout=timeout,
    )
    return payload


def create_mobile_captcha(
    session: requests.Session,
    headers: Mapping[str, str],
    challenge: RiskChallenge,
    *,
    on_aigis: Callable[[dict[str, Any]], str] | None = None,
    on_status: Callable[[str], None] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """``createMobileCaptchaByActionTicket``：下发短信验证码。

    可能先返回 ``-3101`` + ``X-Rpc-Aigis``（抓包里是 ``risk_type=icon`` 的极验点选）；
    此时用 ``on_aigis`` 解出后带 ``x-rpc-aigis`` **重发同一 body**。
    """

    def report(message: str) -> None:
        if on_status is not None:
            on_status(message)

    body = {
        "action_ticket": challenge.action_ticket,
        "action_type": challenge.action_type or ACTION_TYPE_VERIFY_FOR_COMPONENT,
    }
    header_map = dict(headers)
    payload, response = _post_json(
        session, CREATE_MOBILE_CAPTCHA_URL, header_map, body, stage="createMobileCaptcha", timeout=timeout
    )
    if payload.get("retcode") != 0 and response.headers.get("x-rpc-aigis"):
        aigis = parse_aigis_header(response.headers.get("x-rpc-aigis"))
        if on_aigis is None:
            raise RiskVerificationError(
                "发送短信前需要完成极验验证码，但没有提供 on_aigis 回调",
                stage="createMobileCaptcha",
                retcode=payload.get("retcode"),
                aigis=aigis,
                payload=payload,
            )
        risk_type = (aigis or {}).get("data")
        risk_type = risk_type.get("risk_type") if isinstance(risk_type, dict) else None
        report(f"       需要极验验证码 (risk_type={risk_type or '?'}), 等待人工完成 ...")
        header_map["x-rpc-aigis"] = str(on_aigis(aigis or {}))
        report("       已带上 x-rpc-aigis, 重发发送短信请求 ...")
        payload, response = _post_json(
            session, CREATE_MOBILE_CAPTCHA_URL, header_map, body, stage="createMobileCaptcha", timeout=timeout
        )
    return payload


def verify_action_ticket_partly(
    session: requests.Session,
    headers: Mapping[str, str],
    challenge: RiskChallenge,
    code: str,
    *,
    verify_method: int = VERIFY_METHOD_MOBILE,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """``verifyActionTicketPartly``：提交手机短信验证码。

    抓包请求体（133 字节）::

        {"action_ticket": "<32 hex>", "action_type": "verify_for_component",
         "verify_method": 1, "mobile_captcha": "909286"}
    """
    body: dict[str, Any] = {
        "action_ticket": challenge.action_ticket,
        "action_type": challenge.action_type or ACTION_TYPE_VERIFY_FOR_COMPONENT,
        "verify_method": int(verify_method),
    }
    if int(verify_method) == VERIFY_METHOD_MOBILE:
        body["mobile_captcha"] = str(code)
    elif int(verify_method) == VERIFY_METHOD_EMAIL:
        body["email_captcha"] = str(code)
    else:
        body["captcha"] = str(code)
    payload, _ = _post_json(
        session, VERIFY_ACTION_TICKET_PARTLY_URL, headers, body, stage="verifyActionTicketPartly", timeout=timeout
    )
    return payload


def check_risk_verified(
    session: requests.Session,
    headers: Mapping[str, str],
    challenge: RiskChallenge,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """``checkRiskVerified``：确认风控已通过；**登录态 cookie 由这一步下发**。"""
    payload, _ = _post_json(
        session,
        CHECK_RISK_VERIFIED_URL,
        headers,
        {"action_ticket": challenge.risk_ticket},
        stage="checkRiskVerified",
        timeout=timeout,
    )
    return payload


def check_smart_captcha(
    session: requests.Session,
    headers: Mapping[str, str],
    ticket: str,
    *,
    check_data: str | None = None,
    timeout: float = 20.0,
) -> tuple[dict[str, Any], requests.Response]:
    """``/common/aigis/api/checkSmartCaptcha``：``verify_type=1 (GEETEST)`` 支线。

    ``check_data`` 为空时是"我要挑战"，返回 ``{mmt_type, data}``；
    带上 ``base64(JSON(validate))`` 再调一次即为"我解完了"。
    """
    body: dict[str, Any] = {"ticket": ticket}
    if check_data is not None:
        body["check_data"] = check_data
    payload, response = _post_json(
        session, CHECK_SMART_CAPTCHA_URL, headers, body, stage="checkSmartCaptcha", timeout=timeout
    )
    return payload, response


# ---------------------------------------------------------------------------
# 组合流程
# ---------------------------------------------------------------------------
def _raise_nonzero(stage: str, payload: dict[str, Any], hints: str = "") -> None:
    retcode = payload.get("retcode")
    message = payload.get("message")
    detail = f"{stage} 失败: retcode={retcode} message={message}"
    if hints:
        detail += f" —— {hints}"
    raise RiskVerificationError(detail, stage=stage, retcode=retcode, payload=payload)


def _embed(headers: Mapping[str, str], tid: str, verify_method: int | None) -> dict[str, str]:
    """把 referer / x-rpc-mi_referrer 换成 iframe 验证页（其余头沿用登录请求）。"""
    out = dict(headers)
    referer = security_verification_referer(tid, verify_method)
    out["referer"] = LOGIN_ORIGIN
    out["x-rpc-mi_referrer"] = referer
    return out


def complete_identity_verification(
    session: requests.Session,
    headers: Mapping[str, str],
    challenge: RiskChallenge,
    *,
    on_sms_code: Callable[[dict[str, Any]], str],
    on_aigis: Callable[[dict[str, Any]], str] | None = None,
    on_status: Callable[[str], None] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """``verify_type=2 (IDENTITY)`` 全流程，成功返回 ``checkRiskVerified`` 的 ``data``。

    参数:
        session: 与失败的 ``loginByPassword`` **同一个** session（cookie 要连续）。
        headers: 登录请求用的头（函数内部会为 iframe 步骤换 referer）。
        challenge: :func:`parse_verify_header` 的结果。
        on_sms_code: 交互回调，入参 ``{"mobile": 脱敏手机号, "methods": [...], "info": ...}``，
            返回用户输入的短信验证码。
        on_aigis: 发短信触发极验时的人工解验证码回调（见 ``core.aigis``）。
        on_status: 进度回调（默认走 logger）。
        timeout: 单请求超时秒数。
    """
    logger = get_logger("verifier")

    def report(message: str) -> None:
        if on_status is not None:
            on_status(message)
        else:
            logger.info(message)

    tid = secrets.token_hex(4)
    iframe_headers = _embed(headers, tid, None)

    report(f"[风控] 身份验证 (verify_type={challenge.verify_type_name})")

    # 1) markRiskAction —— 失败不致命, 继续走验证（成功判定以 checkRiskVerified 为准）
    marked = mark_risk_action(session, headers, challenge, timeout=timeout)
    if marked.get("retcode") != 0:
        report(f"       警告: markRiskAction retcode={marked.get('retcode')} message={marked.get('message')}")

    # 2) getActionTicketInfo
    info_payload = get_action_ticket_info(session, iframe_headers, challenge, timeout=timeout)
    if info_payload.get("retcode") != 0:
        _raise_nonzero("getActionTicketInfo", info_payload)
    info = info_payload.get("data") or {}
    verify_info = info.get("verify_info") or {}
    user_info = info.get("user_info") or {}
    methods = list(verify_info.get("chosen_methods") or [])
    if not methods:
        for combo in verify_info.get("verify_method_combinations") or []:
            methods.extend(combo.get("verify_methods") or [])
    mobile = user_info.get("mobile") or ""
    report(
        "       验证方式="
        + ", ".join(f"{VERIFY_METHOD_NAMES.get(m, m)}({m})" for m in methods)
        + f", 手机={mobile or '-'}"
    )

    # 3) 按验证方式下发验证码（本轮只实现抓包覆盖的手机短信；其余类型明确报错）
    if VERIFY_METHOD_MOBILE not in methods:
        names = ", ".join(f"{VERIFY_METHOD_NAMES.get(m, m)}({m})" for m in methods) or "无"
        raise RiskVerificationError(
            f"服务端要求的验证方式 [{names}] 暂未实现（样本只覆盖 verify_method=1 手机短信）",
            stage="selectMethod",
            payload=info_payload,
        )
    iframe_headers = _embed(headers, tid, VERIFY_METHOD_MOBILE)
    sent = create_mobile_captcha(
        session, iframe_headers, challenge, on_aigis=on_aigis, on_status=on_status, timeout=timeout
    )
    if sent.get("retcode") != 0:
        _raise_nonzero("createMobileCaptcha", sent, "短信未下发, 请确认手机号可用")
    report(f"       短信验证码已下发至 {mobile or '绑定手机'}")

    # 4) 人工输入验证码
    code = str(on_sms_code({"mobile": mobile, "methods": methods, "info": info}) or "").strip()
    if not code:
        raise RiskVerificationError("没有收到短信验证码输入", stage="verifyActionTicketPartly")
    verified = verify_action_ticket_partly(
        session, iframe_headers, challenge, code, verify_method=VERIFY_METHOD_MOBILE, timeout=timeout
    )
    if verified.get("retcode") != 0:
        _raise_nonzero("verifyActionTicketPartly", verified, "验证码错误或已过期")
    report("       短信验证通过")

    # 5) checkRiskVerified —— 登录态 cookie 在这一步下发
    checked = check_risk_verified(session, headers, challenge, timeout=timeout)
    if checked.get("retcode") != 0:
        _raise_nonzero("checkRiskVerified", checked, "风控未通过, 可能需要重新验证")
    report("       身份验证完成, 登录态已下发")
    return checked.get("data") or {}


def complete_geetest_verification(
    session: requests.Session,
    headers: Mapping[str, str],
    challenge: RiskChallenge,
    *,
    on_aigis: Callable[[dict[str, Any]], str] | None = None,
    on_status: Callable[[str], None] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """``verify_type=1 (GEETEST)`` 支线（**本次抓包未覆盖，按登录页 JS 还原**）。

    ``checkSmartCaptcha({ticket})`` → ``mmt_type`` 为 0 直接通过；为 1 时
    ``data`` 是极验 init 配置，解出后 ``checkSmartCaptcha({ticket, check_data: base64(JSON(validate))})``，
    最后同样以 ``checkRiskVerified({action_ticket: risk_ticket})`` 收尾。
    """
    logger = get_logger("verifier")

    def report(message: str) -> None:
        if on_status is not None:
            on_status(message)
        else:
            logger.info(message)

    report("[风控] 智能验证码 (verify_type=GEETEST)")
    marked = mark_risk_action(session, headers, challenge, timeout=timeout)
    if marked.get("retcode") != 0:
        report(f"       警告: markRiskAction retcode={marked.get('retcode')}")

    payload, _ = check_smart_captcha(session, headers, challenge.action_ticket, timeout=timeout)
    if payload.get("retcode") != 0:
        _raise_nonzero("checkSmartCaptcha", payload)
    data = payload.get("data") or {}
    mmt_type = data.get("mmt_type", 0)
    if mmt_type == 1:
        config = data.get("data")
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {"raw": config}
        if on_aigis is None:
            raise RiskVerificationError(
                "需要完成极验验证码，但没有提供 on_aigis 回调",
                stage="checkSmartCaptcha",
                aigis={"session_id": "", "data": config, "raw": data},
                payload=payload,
            )
        report("       等待人工完成极验验证码 ...")
        header_value = str(on_aigis({"session_id": "", "data": config, "raw": data}))
        check_data = header_value.partition(";")[2] or header_value
        payload, _ = check_smart_captcha(
            session, headers, challenge.action_ticket, check_data=check_data, timeout=timeout
        )
        if payload.get("retcode") != 0:
            _raise_nonzero("checkSmartCaptcha(check_data)", payload)
    elif mmt_type != 0:
        raise RiskVerificationError(
            f"未知 mmt_type={mmt_type}", stage="checkSmartCaptcha", payload=payload
        )

    checked = check_risk_verified(session, headers, challenge, timeout=timeout)
    if checked.get("retcode") != 0:
        _raise_nonzero("checkRiskVerified", checked)
    report("       身份验证完成, 登录态已下发")
    return checked.get("data") or {}


def complete_risk_verification(
    session: requests.Session,
    headers: Mapping[str, str],
    challenge: RiskChallenge,
    *,
    on_sms_code: Callable[[dict[str, Any]], str] | None = None,
    on_aigis: Callable[[dict[str, Any]], str] | None = None,
    on_status: Callable[[str], None] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """按 ``verify_type`` 分派到具体实现。"""
    if challenge.verify_type == VERIFY_TYPE_IDENTITY:
        if on_sms_code is None:
            raise RiskVerificationError(
                "服务端要求身份验证（手机短信），但没有提供 on_sms_code 回调",
                stage="selectMethod",
                payload=challenge.raw,
            )
        return complete_identity_verification(
            session,
            headers,
            challenge,
            on_sms_code=on_sms_code,
            on_aigis=on_aigis,
            on_status=on_status,
            timeout=timeout,
        )
    if challenge.verify_type == VERIFY_TYPE_GEETEST:
        return complete_geetest_verification(
            session, headers, challenge, on_aigis=on_aigis, on_status=on_status, timeout=timeout
        )
    if challenge.verify_type == VERIFY_TYPE_SOFT_FORBIDDEN:
        raise RiskVerificationError(
            "账号被软封禁 (SOFT_FORBIDDEN)，需要在网页端绑定手机后登录",
            stage="selectMethod",
            payload=challenge.raw,
        )
    raise RiskVerificationError(
        f"无法识别的 verify_type={challenge.verify_type}", stage="selectMethod", payload=challenge.raw
    )


__all__ = [
    "ACTION_TYPE_VERIFY_FOR_COMPONENT",
    "CHECK_RISK_VERIFIED_URL",
    "CHECK_SMART_CAPTCHA_URL",
    "CREATE_EMAIL_CAPTCHA_URL",
    "CREATE_MOBILE_CAPTCHA_URL",
    "GET_ACTION_TICKET_INFO_URL",
    "MARK_RISK_ACTION_URL",
    "MODIFY_VERIFY_METHOD_URL",
    "RiskChallenge",
    "RiskVerificationError",
    "SECURITY_VERIFICATION_REFERER",
    "VERIFIER_BASE",
    "VERIFY_ACTION_TICKET_PARTLY_URL",
    "VERIFY_METHOD_EMAIL",
    "VERIFY_METHOD_MOBILE",
    "VERIFY_METHOD_NAMES",
    "VERIFY_TYPE_GEETEST",
    "VERIFY_TYPE_IDENTITY",
    "VERIFY_TYPE_NAMES",
    "VERIFY_TYPE_SOFT_FORBIDDEN",
    "check_risk_verified",
    "check_smart_captcha",
    "complete_geetest_verification",
    "complete_identity_verification",
    "complete_risk_verification",
    "create_mobile_captcha",
    "get_action_ticket_info",
    "mark_risk_action",
    "parse_verify_header",
    "security_verification_referer",
    "verify_action_ticket_partly",
]
