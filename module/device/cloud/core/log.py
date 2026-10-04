from __future__ import annotations

import inspect
import logging
import sys

LOGGER_NAME = "cloud_game"


def get_logger(name: str | None = None) -> logging.Logger:
    """返回项目统一 logger。"""
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def configure_logging(level: int | str = logging.INFO, log_file: str | None = None) -> None:
    """配置默认控制台日志输出。若指定 *log_file*，同时写入文件。"""
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger(LOGGER_NAME)
    root_logger.setLevel(level)
    root_logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", "%H:%M:%S")

    if not root_logger.handlers:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    if log_file is not None:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # 抑制外部库的连接/媒体协商噪声；项目自己的 cloud_game logger 不受影响。
    for noisy in (
        "aioice",
        "aioice.ice",
        "aiortc",
        "aiortc.rtcrtpreceiver",
        "aiortc.rtcrtpsender",
        "aiortc.rtcpeerconnection",
        "aiortc.rtcicetransport",
        "aiortc.rtcdtlstransport",
        "websockets",
        "websockets.client",
        "websockets.server",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def emit_log_callback(callback, message: str, level: int = logging.INFO) -> None:
    """调用日志回调，优先传入 ``(message, level)`` 并兼容旧的单参数回调。"""
    if callback is None:
        return
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        callback(message, level)
        return

    positional_count = 0
    has_varargs = False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            has_varargs = True
        elif parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional_count += 1

    if has_varargs or positional_count >= 2:
        callback(message, level)
    else:
        callback(message)
