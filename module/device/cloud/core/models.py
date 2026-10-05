from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CoreConfig


@dataclass
class CloudGameConfig:
    max_seconds: int = 0
    queue_type: str = ""
    node: str = ""
    # None = 跟随 core_config.platform_profile（桌面键鼠=7；手机触控=8）
    speed_client_type: int | None = None
    snapshot_dir: str | None = None
    snapshot_interval: float = 5.0
    video_frame_interval: float | None = None               # 默认不做处理
    control_actions: list[dict[str, Any]] | None = None
    ws_log_payload: bool = True
    ws_payload_limit: int = 2048
    color: bool = False
    clipboard_getter: Callable[[], str] | None = None
    core_config: CoreConfig | dict[str, Any] | None = None
    root_dir: Path = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class GameTicket:
    finish_result: dict[str, Any]

    @property
    def sdk_param(self) -> str:
        """取出调度结果中的 SDK 启动参数。"""
        return str(self.finish_result.get("sdk_param") or "")


@dataclass(frozen=True)
class InputAction:
    type: str
    x: float = 0.0
    y: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    button: str = "left"
    key_code: int = 0
    delta: float = 0.0
    capslock: bool = False
    numlock: bool = False
    text: str = ""
    # 触控（手机模式）字段
    finger_id: int = 0
    is_primary: bool = False
    pressure: float = 0.0
    events: tuple[dict[str, Any], ...] = ()

    @classmethod
    def mouse_move(cls, x: float, y: float, dx: float = 0.0, dy: float = 0.0) -> "InputAction":
        """生成鼠标移动动作。"""
        return cls(type="move", x=x, y=y, dx=dx, dy=dy)

    @classmethod
    def mouse_down(cls, button: str, x: float, y: float) -> "InputAction":
        """生成鼠标按下动作。"""
        return cls(type="down", button=button, x=x, y=y)

    @classmethod
    def mouse_up(cls, button: str, x: float, y: float) -> "InputAction":
        """生成鼠标释放动作。"""
        return cls(type="up", button=button, x=x, y=y)

    @classmethod
    def scroll(cls, delta: float) -> "InputAction":
        """生成滚轮动作。"""
        return cls(type="scroll", delta=delta)

    @classmethod
    def key_down(cls, key_code: int, capslock: bool = False, numlock: bool = False) -> "InputAction":
        """生成键盘按下动作。"""
        return cls(type="key_down", key_code=key_code, capslock=capslock, numlock=numlock)

    @classmethod
    def key_up(cls, key_code: int, capslock: bool = False, numlock: bool = False) -> "InputAction":
        """生成键盘释放动作。"""
        return cls(type="key_up", key_code=key_code, capslock=capslock, numlock=numlock)

    @classmethod
    def ime(cls, text: str) -> "InputAction":
        """生成 IME 文本输入动作。"""
        return cls(type="ime", text=text)

    @classmethod
    def clipboard(cls, text: str) -> "InputAction":
        """生成剪贴板文本输入动作。"""
        return cls(type="clipboard", text=text)

    # ---- 触控（手机模式）------------------------------------------------
    @classmethod
    def touch_down(
        cls, x: float, y: float, finger_id: int = 0, is_primary: bool = True, pressure: float = 0.0
    ) -> "InputAction":
        """生成触点按下动作（单指默认 primary）。"""
        return cls(type="touch_down", x=x, y=y, finger_id=finger_id, is_primary=is_primary, pressure=pressure)

    @classmethod
    def touch_move(
        cls, x: float, y: float, finger_id: int = 0, is_primary: bool = True, pressure: float = 0.0
    ) -> "InputAction":
        """生成触点移动动作。"""
        return cls(type="touch_move", x=x, y=y, finger_id=finger_id, is_primary=is_primary, pressure=pressure)

    @classmethod
    def touch_up(
        cls, x: float, y: float, finger_id: int = 0, is_primary: bool = True, pressure: float = 0.0
    ) -> "InputAction":
        """生成触点抬起动作。"""
        return cls(type="touch_up", x=x, y=y, finger_id=finger_id, is_primary=is_primary, pressure=pressure)

    @classmethod
    def touch_events(cls, events: list[dict[str, Any]]) -> "InputAction":
        """生成多点触控动作：``events`` 为 ``{"action","x","y","finger_id",...}`` 列表。"""
        return cls(type="touch", events=tuple(events))

    def as_dict(self) -> dict[str, Any]:
        """把输入动作转换为可发送的字典。"""
        return {
            "type": self.type,
            "x": self.x,
            "y": self.y,
            "dx": self.dx,
            "dy": self.dy,
            "button": self.button,
            "key_code": self.key_code,
            "delta": self.delta,
            "capslock": self.capslock,
            "numlock": self.numlock,
            "text": self.text,
            "finger_id": self.finger_id,
            "is_primary": self.is_primary,
            "pressure": self.pressure,
            "events": [dict(event) for event in self.events],
        }
