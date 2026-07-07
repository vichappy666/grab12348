"""12348 广东公共法律服务平台 —— 抢班次接口客户端。

只封装抢班需要的几个接口，使用 token（Bearer）鉴权。
所有请求复用同一个 requests.Session，开启 keep-alive 连接池，
减少 TCP/TLS 握手开销，这是“拼速度”场景里很实在的一块优化。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests


BASE_URL = "https://gd.12348.gov.cn/cloudexamh5/cloudh5api"

# 跟抓包记录保持一致的请求头，尽量贴近真实浏览器，避免被简单规则拦掉。
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://gd.12348.gov.cn",
    "Referer": "https://gd.12348.gov.cn/cloudexamh5/schedulingCenter",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
        "Mobile/15E148 Safari/604.1 Edg/148.0.0.0"
    ),
    "sec-ch-ua": '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"iOS"',
}


@dataclass
class Shift:
    """一个可抢的班次。"""

    scheduling_id: str
    post_name: str
    date: str            # 形如 2026-05-22
    shift_name: str      # 形如 C1（2026-普）
    start_time: str      # 08:15
    end_time: str        # 13:15
    capacity: int        # 总名额 schedulingNumOfPeople
    scheduled: int       # 已抢人数 scheduledNumOfPeople
    grab_flag: str       # "0" 未抢
    language: str = ""

    @property
    def has_room(self) -> bool:
        """是否还有名额。"""
        return self.scheduled < self.capacity

    @property
    def code(self) -> str:
        """班次代码：'C（2026-优）' -> 'C'，'C1（2026-普）' -> 'C1'，'早高峰' -> '早高峰'。

        用于按顺位精确匹配（避免子串匹配把 'C' 误判成 'C1'/'C3'）。
        """
        name = self.shift_name
        for sep in ("（", "("):
            i = name.find(sep)
            if i != -1:
                return name[:i].strip()
        return name.strip()

    @property
    def tier(self) -> str:
        """场次类别：'优' / '普' / ''（如高峰班无此区分）。"""
        if "优" in self.shift_name:
            return "优"
        if "普" in self.shift_name:
            return "普"
        return ""

    @property
    def remaining(self) -> int:
        return max(self.capacity - self.scheduled, 0)

    @property
    def label(self) -> str:
        return f"{self.date} {self.shift_name} {self.start_time}-{self.end_time}"

    @classmethod
    def from_json(cls, d: dict) -> "Shift":
        return cls(
            scheduling_id=d.get("schedulingId", ""),
            post_name=d.get("postName", ""),
            date=(d.get("schedulingDate") or "")[:10],
            shift_name=d.get("shiftName", ""),
            start_time=d.get("startTime", ""),
            end_time=d.get("endTime", ""),
            capacity=int(d.get("schedulingNumOfPeople") or 0),
            scheduled=int(d.get("scheduledNumOfPeople") or 0),
            grab_flag=str(d.get("grabFlag", "0")),
            language=d.get("languageCategory", "") or "",
        )


@dataclass
class GrabResult:
    """一次抢班请求的结果。"""

    ok: bool
    code: int
    msg: str
    scheduling_id: str
    elapsed_ms: float

    # 业务上区分几种典型结果，方便上层决策
    @property
    def is_full(self) -> bool:
        # 名额已满：还能继续抢别的班次
        return self.code == 500 and "超过限制" in (self.msg or "")

    @property
    def is_auth_error(self) -> bool:
        # token 失效：必须停下来重新登录
        return self.code in (401, 403) or "登录" in (self.msg or "") or "token" in (self.msg or "").lower()


class AuthError(Exception):
    """token 失效 / 未登录。"""


class Grab12348Client:
    def __init__(self, token: str, base_url: str = BASE_URL, timeout: float = 20.0,
                 retries: int = 2, retry_backoff: float = 0.5):
        if not token:
            raise ValueError("token 不能为空，请先登录后从浏览器复制 token")
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 政府服务器响应慢且不稳（实测偶尔 >20s 才回），GET 类请求超时/网络错时自动重试。
        self.retries = retries
        self.retry_backoff = retry_backoff

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        # token 同时通过 cookie 传，跟抓包记录一致
        self.session.cookies.set("token", self.token)

    # ----- 带重试的 GET -----
    def _get(self, url: str) -> requests.Response:
        """GET 请求，超时/网络错误时自动重试（服务器慢，别一超时就放弃）。"""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = e
                if attempt < self.retries and self.retry_backoff:
                    time.sleep(self.retry_backoff)
        assert last_exc is not None
        raise last_exc

    # ----- 校验登录 -----
    def check_login(self) -> dict:
        """校验 token 是否有效，返回当前用户信息。无效则抛 AuthError。"""
        url = f"{self.base_url}/system/user/getCurrentUserDetail"
        r = self._get(url)
        data = _safe_json(r)
        if r.status_code in (401, 403) or data.get("code") in (401, 403):
            raise AuthError("token 已失效，请重新登录获取新 token")
        user = data.get("data") or {}
        if not user.get("phoneNumber") and not user.get("userName"):
            raise AuthError("无法获取用户信息，token 可能已失效")
        return user

    # ----- 查可抢班次列表 -----
    def get_grab_list(self, date: str) -> list[Shift]:
        """查询某天可抢的班次。date 形如 2026-05-22。"""
        url = f"{self.base_url}/business/scheduling/getGrabSchedulingList/{date}"
        r = self._get(url)
        data = _safe_json(r)
        if data.get("code") in (401, 403):
            raise AuthError("token 已失效")
        rows = data.get("data") or []
        return [Shift.from_json(x) for x in rows if isinstance(x, dict)]

    # ----- 已排班 / 已满 的日期（辅助查看） -----
    def get_arranged_dates(self, month: str) -> list[str]:
        """已给我排上的日期。month 形如 2026-05。"""
        url = f"{self.base_url}/business/scheduling/getArrangeScheduling/{month}"
        r = self._get(url)
        return _safe_json(r).get("data") or []

    def get_full_dates(self, month: str) -> list[str]:
        """已抢满的日期。"""
        url = f"{self.base_url}/business/scheduling/getArrangeSchedulingFull/{month}"
        r = self._get(url)
        return _safe_json(r).get("data") or []

    # ----- 核心：抢班 -----
    def grab(self, scheduling_id: str) -> GrabResult:
        """抢指定班次。返回 GrabResult。"""
        url = f"{self.base_url}/business/scheduling/grabScheduling/{scheduling_id}"
        t0 = time.perf_counter()
        try:
            r = self.session.post(
                url, headers={"Content-Length": "0"}, timeout=self.timeout
            )
        except requests.RequestException as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return GrabResult(False, -1, f"请求异常: {e}", scheduling_id, elapsed)
        elapsed = (time.perf_counter() - t0) * 1000
        data = _safe_json(r)
        code = int(data.get("code") or r.status_code)
        msg = data.get("msg") or ""
        ok = code == 200
        return GrabResult(ok, code, msg, scheduling_id, elapsed)


def _safe_json(resp: requests.Response) -> dict:
    try:
        j = resp.json()
        return j if isinstance(j, dict) else {"data": j}
    except ValueError:
        return {"code": resp.status_code, "msg": resp.text[:200], "data": None}
