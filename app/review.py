"""车距复核引擎:基于已落库的逐采样点匹配明细,回放两车在有向路段上的里程-时间线。

复核规则(与业务约定一一对应):
1. 相邻采样点之间按匹配里程比例分配时间,得到任意时刻各车距所在路段起点的里程;
   同一辆车相邻采样点匹配到同一有向路段的同一里程时,期间按原地停车处理
   (里程恒为该值,不做除零插值)。
2. 两车同时位于同一有向路段时,车距 = 两车里程差的绝对值;否则该时刻不计车距。
3. 出现以下情况立即断开当前时段:
   - 路段切换(任一车离开所在有向路段,含跨越采样区间的中途切换);
   - 匹配缺口(任一车处于未匹配区间,包括区间与缺口时间范围重叠);
   - 同刻位置跳变(同一采样时刻出现两个不同的匹配位置)。
4. 仅返回车距不超过阈值且持续时间严格大于零的时段,给出起止 UTC 时刻、
   路段号与该时段内的最小车距;没有符合条件的时段返回空列表。

本模块只做纯计算,输入为从数据库读出的明细,不直接访问数据库。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime

from .util import fmt_ts

EPS_M = 1e-6            # 里程相等判定(米)
EPS_S = 1e-9            # 持续时间判定(秒)


@dataclass(frozen=True)
class PointMatch:
    """单个采样点的匹配明细(对应 analysis_point_matches 一行)。"""
    seq: int
    ts: datetime
    segment_id: str | None   # None 表示缺口内未匹配
    offset_m: float | None


@dataclass(frozen=True)
class Gap:
    """匹配缺口(对应 analysis_gaps 一行)。"""
    start_time: datetime
    end_time: datetime


@dataclass(frozen=True)
class Phase:
    """一辆车在一个采样区间 [t0, t1] 内的回放状态。

    kind:
      parked   原地停车:相邻采样点匹配到同一有向路段的同一里程,里程恒定;
      moving   同一有向路段内顺向移动,里程随时间按里程比例线性分配;
      switch   区间内发生路段切换,该区间不能用于复核(在切换处断开);
      gap      区间与匹配缺口重叠或端点未匹配,该区间不能用于复核;
      jump     零长标记相位:同刻位置跳变时刻,任何覆盖该时刻的区间都必须断开。
    """
    t0: datetime
    t1: datetime
    kind: str
    segment_id: str | None = None
    off0: float = 0.0
    off1: float = 0.0

    def offset_at(self, t: datetime) -> float:
        """t 时刻距所在路段起点的里程(调用方仅在 parked/moving 区间上调用)。"""
        if self.kind == "parked":
            return self.off0
        span = (self.t1 - self.t0).total_seconds()
        if span <= EPS_S:
            return self.off0
        frac = (t - self.t0).total_seconds() / span
        return self.off0 + (self.off1 - self.off0) * frac


def build_phases(matches: list[PointMatch], gaps: list[Gap]) -> tuple[list[Phase], set[datetime]]:
    """把一辆车的逐点明细展开为时间有序的回放区间,并返回必须断开的时刻集合。

    断开始刻集合只收录"同刻位置跳变":同一时刻出现两个不同匹配位置,
    跳变时刻同时落一条零长 jump 相位,使覆盖该时刻的事件区间全部失效;
    跳变时刻前后即使路段恰好相同也不得合并为同一时段。
    """
    pts = sorted(matches, key=lambda m: (m.ts, m.seq))
    phases: list[Phase] = []
    breakpoints: set[datetime] = set()
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        if a.ts == b.ts:
            # 同刻重复:位置一致视为同一采样;位置不一致即同刻位置跳变,在该时刻断开
            if (a.segment_id != b.segment_id
                    or _offset_diff(a.offset_m, b.offset_m) > EPS_M):
                breakpoints.add(a.ts)
                phases.append(Phase(a.ts, a.ts, "jump"))
            continue
        phases.append(_phase_between(a, b, gaps))
    phases.sort(key=lambda p: (p.t0, p.t1))
    return phases, breakpoints


def _phase_between(a: PointMatch, b: PointMatch, gaps: list[Gap]) -> Phase:
    """构造相邻采样点 a -> b 之间的回放区间。"""
    if a.segment_id is None or b.segment_id is None:
        return Phase(a.ts, b.ts, "gap")
    # 区间与任一缺口时间范围重叠 -> 处于匹配缺口中,断开
    for g in gaps:
        if g.start_time < b.ts and a.ts < g.end_time:
            return Phase(a.ts, b.ts, "gap")
    if a.segment_id != b.segment_id:
        # 路段切换:具体切换时刻未知,整个采样区间不参与复核(在区间两端断开)
        return Phase(a.ts, b.ts, "switch")
    if _offset_diff(a.offset_m, b.offset_m) <= EPS_M:
        # 同一有向路段的同一里程:原地停车,期间里程恒定
        return Phase(a.ts, b.ts, "parked", a.segment_id,
                     a.offset_m or 0.0, a.offset_m or 0.0)
    return Phase(a.ts, b.ts, "moving", a.segment_id,
                 a.offset_m or 0.0, b.offset_m or 0.0)


def _offset_diff(x: float | None, y: float | None) -> float:
    if x is None or y is None:
        return float("inf")
    return abs(x - y)


def _covering(phases: list[Phase], t: datetime) -> Phase | None:
    """找到覆盖时刻 t 的区间(phases 按时间有序;零长相位不覆盖任何正长区间)。"""
    starts = [p.t0 for p in phases]
    i = bisect_right(starts, t) - 1
    while i >= 0 and phases[i].t1 <= t:
        i -= 1
    return phases[i] if i >= 0 else None


@dataclass
class ClosePeriod:
    """一段车距不超过阈值的连续时段。"""
    start_time: datetime
    end_time: datetime
    segment_id: str
    min_distance_m: float


def review_distance(
    matches_a: list[PointMatch],
    gaps_a: list[Gap],
    matches_b: list[PointMatch],
    gaps_b: list[Gap],
    threshold_m: float,
) -> list[ClosePeriod]:
    """复核两车车距,返回车距 <= threshold_m 且持续时间 > 0 的各段(按起始时刻排序)。"""
    phases_a, bp_a = build_phases(matches_a, gaps_a)
    phases_b, bp_b = build_phases(matches_b, gaps_b)
    breakpoints = bp_a | bp_b

    # 合并时间轴:取两车全部采样时刻作为事件点,事件点之间两车的里程都是线性/恒定的
    events = sorted({m.ts for m in matches_a} | {m.ts for m in matches_b})

    periods: list[ClosePeriod] = []
    cur: ClosePeriod | None = None

    for t0, t1 in zip(events, events[1:]):
        # 零长事件区间直接跳过;区间起点落在跳变时刻上则该区间失效
        # (跳变时刻之前的区间在跳变发生前已结束,仍然有效,故只检查起点)
        if (t1 - t0).total_seconds() <= EPS_S or t0 in breakpoints:
            cur = _close(periods, cur)
            continue
        pa = _covering(phases_a, t0)
        pb = _covering(phases_b, t0)
        # 任一车在该区间处于缺口/路段切换/同刻跳变,或两车不在同一有向路段 -> 断开
        if (pa is None or pb is None
                or pa.kind not in ("parked", "moving")
                or pb.kind not in ("parked", "moving")
                or pa.segment_id != pb.segment_id):
            cur = _close(periods, cur)
            continue
        seg = pa.segment_id
        da0, da1 = pa.offset_at(t0), pa.offset_at(t1)
        db0, db1 = pb.offset_at(t0), pb.offset_at(t1)
        lo, hi, min_d = _feasible_window(t0, t1, da0, da1, db0, db1, threshold_m)
        if lo is None:
            cur = _close(periods, cur)
            continue
        if (hi - lo).total_seconds() <= EPS_S:
            # 仅在孤立瞬间满足阈值(穿越点),持续时间为零,不计
            cur = _close(periods, cur)
            continue
        # 与前一时段连续(事件边界相接、同一路段)则合并,否则另起一段
        if cur is not None and cur.segment_id == seg and cur.end_time == lo:
            cur.end_time = hi
            cur.min_distance_m = min(cur.min_distance_m, min_d)
        else:
            cur = _close(periods, cur)
            cur = ClosePeriod(lo, hi, seg, min_d)

    _close(periods, cur)
    return periods


def _close(periods: list[ClosePeriod], cur: ClosePeriod | None) -> None:
    if cur is not None and (cur.end_time - cur.start_time).total_seconds() > EPS_S:
        periods.append(cur)
    return None


def _feasible_window(
    t0: datetime, t1: datetime,
    da0: float, da1: float, db0: float, db1: float,
    threshold_m: float,
) -> tuple[datetime | None, datetime | None, float]:
    """在区间 [t0, t1] 内求 |里程差| <= 阈值的子区间与其中最小车距。

    两车里程在区间内均为时间的线性函数,故差值 d(x) = d0 + (d1-d0)*x
    (x ∈ [0,1] 为时间比例)也是线性的;可行域为凸集,按两条线性边界
    d = ±threshold 各裁剪一次即可。
    返回 (起, 止, 最小车距);无可行(正长)区间时起为 None。
    """
    d0, d1 = da0 - db0, da1 - db1
    delta = d1 - d0
    lo, hi = 0.0, 1.0

    if abs(delta) <= EPS_M:
        # 差值恒定(含两车都停车的情形)
        if abs(d0) <= threshold_m + EPS_M:
            return t0, t1, round(min(abs(d0), threshold_m), 3)
        return None, None, 0.0

    # d(x) = threshold 与 d(x) = -threshold 的交点比例
    x_hi = (threshold_m - d0) / delta
    x_lo = (-threshold_m - d0) / delta
    if delta > 0:
        hi = min(hi, x_hi)
        lo = max(lo, x_lo)
    else:
        lo = max(lo, x_hi)
        hi = min(hi, x_lo)

    if hi - lo <= EPS_S or hi <= 0.0 or lo >= 1.0:
        return None, None, 0.0
    lo, hi = max(0.0, lo), min(1.0, hi)
    if hi - lo <= EPS_S:
        return None, None, 0.0

    # 最小 |d|:零点比例落在可行区间内则最小车距为 0,否则取两端较小者
    x_zero = -d0 / delta
    if lo <= x_zero <= hi:
        min_d = 0.0
    else:
        min_d = min(abs(d0 + delta * lo), abs(d0 + delta * hi))
    min_d = max(0.0, min(min_d, threshold_m))
    span = t1 - t0
    return (t0 + span * lo, t0 + span * hi, round(min_d, 3))


def serialize_periods(periods: list[ClosePeriod]) -> list[dict]:
    """格式化为 API 响应(UTC ISO8601,毫秒精度)。"""
    return [
        {
            "start_time": fmt_ts(p.start_time),
            "end_time": fmt_ts(p.end_time),
            "duration_s": round((p.end_time - p.start_time).total_seconds(), 3),
            "segment_id": p.segment_id,
            "min_distance_m": round(p.min_distance_m, 3),
        }
        for p in periods
    ]
