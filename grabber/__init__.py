"""12348 抢班次工具包。"""
from .client import Grab12348Client, Shift, GrabResult, AuthError
from .targeting import TargetSpec
from .grabber import Grabber, GrabberConfig

__all__ = [
    "Grab12348Client",
    "Shift",
    "GrabResult",
    "AuthError",
    "TargetSpec",
    "Grabber",
    "GrabberConfig",
]
