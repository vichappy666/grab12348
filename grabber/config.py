"""读取 YAML 配置并转成各模块需要的对象。"""
from __future__ import annotations

from pathlib import Path

import yaml

from .client import Grab12348Client, BASE_URL
from .grabber import GrabberConfig
from .targeting import TargetSpec


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {path}。请先把 config.example.yaml 复制成 config.yaml 并填写。"
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def build_client(cfg: dict) -> Grab12348Client:
    auth = cfg.get("auth", {})
    token = (auth.get("token") or "").strip()
    if not token or token.startswith("在这里"):
        raise ValueError(
            "请在 config.yaml 的 auth.token 填入有效 token。\n"
            "获取方法见 README：登录后从浏览器开发者工具里复制 Authorization 的 Bearer 值。"
        )
    base_url = auth.get("base_url") or BASE_URL
    return Grab12348Client(token=token, base_url=base_url)


def build_target(cfg: dict) -> TargetSpec:
    t = cfg.get("target", {})
    dates = t.get("dates") or []
    if isinstance(dates, str):
        dates = [dates]
    dates = [str(d).strip() for d in dates if str(d).strip()]
    if not dates:
        raise ValueError("请在 config.yaml 的 target.dates 至少填一个日期，如 2026-05-22")

    shift_names = t.get("shift_names") or []
    if isinstance(shift_names, str):
        shift_names = [shift_names]
    shift_names = [str(s).strip() for s in shift_names if str(s).strip()]

    return TargetSpec(
        dates=dates,
        shift_names=shift_names,
        time_after=str(t.get("time_after") or "").strip(),
        time_before=str(t.get("time_before") or "").strip(),
    )


def build_grabber_config(cfg: dict) -> GrabberConfig:
    g = cfg.get("grab", {})
    return GrabberConfig(
        poll_interval=float(g.get("poll_interval", 0.25)),
        duration=float(g.get("duration", 120)),
        parallel=int(g.get("parallel", 3)),
        stop_after=int(g.get("stop_after", 1)),
        lead_ms=int(g.get("lead_ms", 300)),
        aggressive=bool(g.get("aggressive", False)),
    )
