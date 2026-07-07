"""读取 YAML 配置并转成各模块需要的对象。"""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import yaml

from . import scheduler
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
    timeout = float(auth.get("timeout") or 20.0)
    return Grab12348Client(token=token, base_url=base_url, timeout=timeout)


def _str_list(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def build_target(cfg: dict) -> TargetSpec:
    t = cfg.get("target", {})
    dates = _str_list(t.get("dates"))
    if not dates:
        raise ValueError("请在 config.yaml 的 target.dates 至少填一个日期，或填 auto")

    order = _str_list(t.get("order"))
    order_friday = _str_list(t.get("order_friday"))
    if not order and not order_friday:
        raise ValueError("请在 config.yaml 的 target.order / order_friday 配置班次顺位，如 [C, B, D]")

    prefer = str(t.get("prefer") or "优").strip()
    return TargetSpec(dates=dates, order=order, order_friday=order_friday, prefer=prefer)


def resolve_auto_dates(target: TargetSpec, ref: dt.datetime) -> TargetSpec:
    """把 dates 里的 "auto" 按放班规则展开成具体日期（以 ref 时刻所属的放班场次为准）。

    可与写死的日期混用，展开后去重、保序；不含 "auto" 则原样返回。
    """
    if not any(d.lower() == "auto" for d in target.dates):
        return target
    window = scheduler.release_dates(ref)
    dates: list[str] = []
    for d in target.dates:
        expanded = window if d.lower() == "auto" else [d]
        for x in expanded:
            if x in dates:
                continue
            if not target.order_for(x):   # 那天没有顺位表（如周六/日）= 不抢，跳过
                continue
            dates.append(x)
    return replace(target, dates=dates)


def build_grabber_config(cfg: dict) -> GrabberConfig:
    g = cfg.get("grab", {})
    return GrabberConfig(
        poll_interval=float(g.get("poll_interval", 0.25)),
        duration=float(g.get("duration", 120)),
        parallel=int(g.get("parallel", 3)),
        stop_after=int(g.get("stop_after", 1)),
        lead_ms=int(g.get("lead_ms", 300)),
        aggressive=bool(g.get("aggressive", False)),
        per_day=bool(g.get("per_day", True)),
    )
