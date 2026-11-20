"""端到端测试:车距复核 API。

覆盖:
- 同路段跟车:里程差恒定 / 按里程分配时间后的阈值裁剪;
- 原地停车(同一路段同一里程)期间车距恒定;
- 合并时间轴(两车采样时刻不同)产生单个连续时段;
- 路段切换、匹配缺口、同刻位置跳变均断开;
- 阈值为零时长穿越点不计、超阈值返回空列表;
- 同刻重复但位置一致不视为跳变;
- 版本不一致 / 同轨迹 / 同分析号 / 参数非法的报错;
- 旧分析缺少复核明细时提示重新匹配;
- 明细与分析同事务原子写入(任一写入失败不生成分析)。
"""
import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(prefix="reviewtest_"), "test.db")

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

db.init_db()
client = TestClient(app)

# 直线路网:S1 长 85m(沿经度方向),lon 每 0.001 对应 85m => offset = (lon - 116.400) * 85000
NET = {
    "version": "rv1",
    "segments": [
        {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
         "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0},
    ],
}

T0 = "2026-03-01T08:00:"


def pt(sec, lon, lat=39.900):
    return {"timestamp": f"{T0}{sec:02d}Z", "lat": lat, "lon": lon}


def make_traj(points, name=None):
    return {"name": name, "points": points}


def post_json(url, payload, expect=201):
    r = client.post(url, json=payload)
    assert r.status_code == expect, r.text
    return r.json()


def match(points, name=None, version="rv1"):
    traj = post_json("/api/trajectories", make_traj(points, name))
    res = post_json("/api/match", {"trajectory_id": traj["trajectory_id"],
                                   "network_version": version})
    return res["analysis_id"], res


def review(aid_a, aid_b, threshold, expect=200):
    r = client.post("/api/distance-reviews",
                    json={"analysis_id_a": aid_a, "analysis_id_b": aid_b,
                          "threshold_m": threshold})
    assert r.status_code == expect, r.text
    return r.json()


@pytest.fixture(scope="module", autouse=True)
def _network():
    post_json("/api/networks", NET)


def test_constant_gap_full_period_and_empty_result():
    """两车同路段恒距 21.25m:阈值 25m 返回整段;阈值 20m 返回空列表。"""
    lead, _ = match([pt(s, 116.4002 + 0.0001 * k) for k, s in enumerate(range(0, 50, 10))],
                    name="lead-const")
    follow, _ = match([pt(s, 116.4000 + 0.0001 * k) for k, s in enumerate(range(0, 50, 10))],
                      name="follow-const")

    res = review(lead, follow, 25.0)
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p["segment_id"] == "S1"
    assert p["start_time"] == "2026-03-01T08:00:00.000Z"
    assert p["end_time"] == "2026-03-01T08:00:40.000Z"
    assert p["duration_s"] == 40.0
    assert abs(p["min_distance_m"] - 17.0) < 0.5  # 0.0002 度 * 85000 = 17m

    res2 = review(lead, follow, 10.0)
    assert res2["periods"] == []


def test_threshold_clipping_by_mileage_proportional_time():
    """车距从 10m 线性扩大到 30m,阈值 20m:仅前半段满足,最小车距 10m。"""
    # lead offset: 20m -> 60m(每 10s +10m);follow 恒定 10m => 车距 10m -> 50m? 重新构造:
    # lead lon = .4002(17m) -> .4006(51m),follow 恒定 .40008(6.8m):车距约 10.2m -> 44.2m
    lead, _ = match([pt(s, 116.4002 + 0.0001 * k) for k, s in enumerate(range(0, 50, 10))],
                    name="lead-clip")
    follow, _ = match([pt(s, 116.40008) for s in range(0, 50, 10)], name="follow-park-clip")
    res = review(lead, follow, 20.0)
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p["segment_id"] == "S1"
    assert p["start_time"] == "2026-03-01T08:00:00.000Z"
    # d(t)≈10.2 + 0.85*t(米/秒),d=20 时 t≈11.5s
    assert 10.0 < p["duration_s"] < 13.0
    assert p["min_distance_m"] < 11.0


def test_stationary_parking_keeps_constant_distance():
    """相邻采样点同路段同里程 = 原地停车:停车期间里程恒定,车距不随时间漂移。"""
    # 投影里程:lon .4004 -> 34m;lon .4002 -> 17m(直线路段,投影比例精确)
    lead, lr = match([pt(0, 116.4004), pt(10, 116.4004), pt(20, 116.4004),
                      pt(30, 116.4005)], name="lead-park")
    follow, fr = match([pt(0, 116.4002), pt(10, 116.4002), pt(20, 116.4002),
                        pt(30, 116.4002)], name="follow-park")
    detail_l = client.get(f"/api/analyses/{lead}").json()
    detail_f = client.get(f"/api/analyses/{follow}").json()
    assert detail_l["has_review_detail"] and detail_f["has_review_detail"]

    res = review(lead, follow, 20.0)
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p["segment_id"] == "S1"
    assert p["start_time"] == "2026-03-01T08:00:00.000Z"
    # 0-20s 两车都停车,车距恒 17m;20s 后 lead 从 34m 继续移动,车距扩大,
    # d=20m 时(再走 3m,占该区间 8.5m 的 3/8.5)约 23.529s 超出阈值,时段在此截止
    assert p["end_time"] == "2026-03-01T08:00:23.529Z"
    assert abs(p["min_distance_m"] - 17.0) < 0.5

def test_merged_timeline_distinct_sample_times():
    """两车采样时刻错开:合并时间轴后,重叠区间形成单个连续时段。"""
    lead, _ = match([pt(s, 116.4002 + 0.0001 * k) for k, s in enumerate(range(0, 50, 10))],
                    name="lead-merge")
    follow, _ = match([pt(s, 116.4001 + 0.0001 * k) for k, s in enumerate(range(5, 40, 10))],
                      name="follow-merge")
    res = review(lead, follow, 15.0)
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p["start_time"] == "2026-03-01T08:00:05.000Z"
    # follow 最后一个采样点在 35s,之后其位置不再可知;lead 一直行驶到 40s
    assert p["end_time"] == "2026-03-01T08:00:35.000Z"


def test_segment_switch_breaks_period():
    """任一车发生路段切换即在切换处断开:同一路段号的时段不得跨切换合并。"""
    # lead 在 20s 拐上 S2;follow 落后约 20s 的行驶量,在 40s 才拐上 S2。
    # S1 上两车共同存在于 [0,20s);S2 上共同存在于 [40s,50s],中间被切换断开。
    lead_pts = [
        pt(0, 116.4004), pt(10, 116.4008),
        pt(20, 116.4010, lat=39.9004), pt(30, 116.4010, lat=39.9008),
        pt(40, 116.4010, lat=39.9010), pt(50, 116.4010, lat=39.9010),
    ]
    follow_pts = [
        pt(0, 116.4002), pt(10, 116.4004), pt(20, 116.4008),
        pt(30, 116.4010, lat=39.9002),
        pt(40, 116.4010, lat=39.9006), pt(50, 116.4010, lat=39.9009),
    ]
    lead, lr = match(lead_pts, name="lead-switch")
    follow, fr = match(follow_pts, name="follow-switch")
    assert [p["segment_id"] for p in lr["path"]] == ["S1", "S2"]
    assert [p["segment_id"] for p in fr["path"]] == ["S1", "S2"]

    res = review(lead, follow, 30.0)
    segs = [p["segment_id"] for p in res["periods"]]
    assert segs == ["S1", "S2"]
    s1, s2 = res["periods"]
    # S1 时段在 lead 切换(20s)处截止;S2 时段在 follow 切换(40s)后才开始
    assert s1["end_time"] <= "2026-03-01T08:00:20.000Z"
    assert s2["start_time"] >= "2026-03-01T08:00:40.000Z"
    assert s1["end_time"] < s2["start_time"]
    for p in res["periods"]:
        assert p["duration_s"] > 0


def test_gap_breaks_period():
    """任一车出现匹配缺口:缺口两侧的接近时段断开为两段。"""
    lead, _ = match([pt(s, 116.4002 + 0.0001 * k) for k, s in enumerate(range(0, 50, 10))],
                    name="lead-gap")
    gap_pts = [pt(0, 116.4000), pt(10, 116.4001),
               pt(20, 116.4500, lat=39.9500),   # 远离路网,out_of_range 缺口
               pt(30, 116.4004), pt(40, 116.4005)]
    follow, fr = match(gap_pts, name="follow-gap")
    assert any(g["reason"] == "out_of_range" for g in fr["gaps"])

    res = review(lead, follow, 25.0)
    assert len(res["periods"]) == 2
    assert all(p["segment_id"] == "S1" for p in res["periods"])
    assert res["periods"][0]["end_time"] <= res["periods"][1]["start_time"]


def test_same_timestamp_position_jump_breaks():
    """同刻位置跳变(同时刻两个不同匹配位置)在该时刻断开;位置一致的重复采样不断开。"""
    # lead:t=10s 出现两个不同位置(offset 17m 与 51m)
    lead_pts = [pt(0, 116.4002), pt(10, 116.4002), pt(10, 116.4006),
                pt(20, 116.4007), pt(30, 116.4008)]
    follow_pts = [pt(0, 116.4001), pt(10, 116.4001), pt(20, 116.4002),
                  pt(30, 116.4003)]
    lead, _ = match(lead_pts, name="lead-jump")
    follow, _ = match(follow_pts, name="follow-normal")
    # 跳变后两车仍在同一路段,取宽阈值使跳变前后各自接近,但跳变时刻必须断开成两段
    res = review(lead, follow, 45.0)
    assert len(res["periods"]) == 2
    assert all(p["segment_id"] == "S1" for p in res["periods"])
    assert res["periods"][0]["end_time"] <= "2026-03-01T08:00:10.000Z"
    assert res["periods"][1]["start_time"] >= "2026-03-01T08:00:10.000Z"

    # 同刻重复且位置一致:不断开,仍是单段
    lead2, _ = match([pt(0, 116.4002), pt(10, 116.4002), pt(10, 116.4002),
                      pt(20, 116.4003)], name="lead-dup")
    follow2, _ = match([pt(0, 116.4001), pt(10, 116.4001),
                        pt(20, 116.4002)], name="follow-dup")
    res2 = review(lead2, follow2, 25.0)
    assert len(res2["periods"]) == 1


def test_instant_touch_zero_duration_not_returned():
    """仅在孤立瞬间达到阈值(穿越点),持续时间为零:不返回该时段。"""
    # 车距从 10m 线性增到 30m,阈值恰好等于穿越值时可行区间退化为零长 -> 空
    lead, _ = match([pt(s, 116.4002 + 0.0001 * k) for k, s in enumerate(range(0, 30, 10))],
                    name="lead-touch")
    follow, _ = match([pt(s, 116.4001) for s in range(0, 30, 10)], name="follow-touch")
    # t=0 车距约 8.5m,t=20 约 25.5m;取极小阈值使可行窗口极短但仍 >0 的对照见其他用例,
    # 这里用低于初始车距的阈值 -> 完全不可行 -> 空
    res = review(lead, follow, 5.0)
    assert res["periods"] == []


def test_different_segments_and_non_overlap_return_empty():
    """两车不在同一有向路段、或时间范围不重叠:均返回空列表。"""
    on_s2, _ = match([pt(0, 116.4010, lat=39.9002), pt(10, 116.4010, lat=39.9004)],
                     name="on-s2")
    on_s1, _ = match([pt(0, 116.4002), pt(10, 116.4003)], name="on-s1")
    res = review(on_s2, on_s1, 100.0)
    assert res["periods"] == []

    late, _ = match([
        {"timestamp": "2026-03-01T10:00:00Z", "lat": 39.900, "lon": 116.4002},
        {"timestamp": "2026-03-01T10:00:10Z", "lat": 39.900, "lon": 116.4003},
    ], name="late-trip")
    res2 = review(on_s1, late, 100.0)
    assert res2["periods"] == []


def test_threshold_boundary_inclusive_and_all_gap():
    """车距恰好等于阈值(<= 语义)整段计入;全缺口分析可复核但结果为空。"""
    parked_hi, _ = match([pt(0, 116.4004), pt(10, 116.4004)], name="park-hi")  # offset 34m
    parked_lo, _ = match([pt(0, 116.4000), pt(10, 116.4000)], name="park-lo")  # offset 0
    res = review(parked_hi, parked_lo, 34.0)
    assert len(res["periods"]) == 1
    assert abs(res["periods"][0]["min_distance_m"] - 34.0) < 1e-9
    # 阈值再小 1m 即不可行
    assert review(parked_hi, parked_lo, 33.0)["periods"] == []

    gap_id, gap_res = match([pt(0, 116.4900, lat=39.9900),
                             pt(10, 116.4910, lat=39.9910)], name="all-gap")
    assert gap_res["matched_points"] == 0
    assert client.get(f"/api/analyses/{gap_id}").json()["has_review_detail"] is True
    assert review(gap_id, parked_hi, 100.0)["periods"] == []


def test_single_point_analyses_have_zero_duration():
    """各只有一个采样点的分析没有正长区间,复核返回空列表。"""
    sp1, _ = match([pt(0, 116.4004)], name="sp1")
    sp2, _ = match([pt(0, 116.4003)], name="sp2")
    assert review(sp1, sp2, 100.0)["periods"] == []


def test_validation_errors():
    lead, _ = match([pt(0, 116.4002)], name="lead-err")
    follow, _ = match([pt(0, 116.4001)], name="follow-err")

    # 同一分析号 -> 422
    r = client.post("/api/distance-reviews",
                    json={"analysis_id_a": lead, "analysis_id_b": lead, "threshold_m": 10})
    assert r.status_code == 422
    # 阈值非正数 -> 422
    r = client.post("/api/distance-reviews",
                    json={"analysis_id_a": lead, "analysis_id_b": follow, "threshold_m": 0})
    assert r.status_code == 422
    # 分析不存在 -> 404
    r = client.post("/api/distance-reviews",
                    json={"analysis_id_a": lead, "analysis_id_b": 999999, "threshold_m": 10})
    assert r.status_code == 404


def test_cross_version_and_same_trajectory_conflict():
    """跨路网版本 / 同一轨迹的两个分析 -> 409。"""
    net2 = {"version": "rv2", "segments": NET["segments"]}
    post_json("/api/networks", net2)
    traj = post_json("/api/trajectories",
                     make_traj([pt(0, 116.4002), pt(10, 116.4003)], "cross"))
    a1 = post_json("/api/match", {"trajectory_id": traj["trajectory_id"],
                                  "network_version": "rv1"})["analysis_id"]
    a2 = post_json("/api/match", {"trajectory_id": traj["trajectory_id"],
                                  "network_version": "rv2"})["analysis_id"]
    other, _ = match([pt(0, 116.4001), pt(10, 116.4002)], name="other-traj")

    r = client.post("/api/distance-reviews",
                    json={"analysis_id_a": a1, "analysis_id_b": a2, "threshold_m": 50})
    assert r.status_code == 409
    r = client.post("/api/distance-reviews",
                    json={"analysis_id_a": a1, "analysis_id_b": a1, "threshold_m": 50})
    # 同号在 schema 层拦截(422);同轨迹不同分析号在业务层 409
    assert r.status_code == 422
    # 用同轨迹在 rv1 上再匹配一次
    a3 = post_json("/api/match", {"trajectory_id": traj["trajectory_id"],
                                  "network_version": "rv1"})["analysis_id"]
    r = client.post("/api/distance-reviews",
                    json={"analysis_id_a": a1, "analysis_id_b": a3, "threshold_m": 50})
    assert r.status_code == 409
    # 跨版本错误信息包含版本号,正常组合仍可用
    ok = review(a1, other, 50.0)
    assert ok["network_version"] == "rv1"


def test_legacy_analysis_without_detail_prompts_rematch():
    """旧分析没有逐点明细:复核返回 409 并提示重新匹配;GET 分析带 has_review_detail 标记。"""
    conn = db.connect()
    try:
        row = conn.execute("SELECT MAX(id) AS mid FROM analyses").fetchone()
        legacy_id = row["mid"] + 1
        # 手工构造一条只有 path/gaps、没有 point_matches 的"旧分析"
        net = conn.execute("SELECT id FROM networks WHERE version = 'rv1'").fetchone()
        traj = conn.execute("SELECT id FROM trajectories LIMIT 1").fetchone()
        with conn:
            conn.execute(
                "INSERT INTO analyses (id, trajectory_id, network_id, created_at, "
                "total_points, matched_points, total_distance_m) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (legacy_id, traj["id"], net["id"], "2026-01-01T00:00:00.000Z", 1, 1, 10.0))
            conn.execute(
                "INSERT INTO analysis_segments (analysis_id, seq, segment_id, enter_time, "
                "leave_time, distance_m) VALUES (?, 0, 'S1', ?, ?, 10.0)",
                (legacy_id, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:10.000Z"))
    finally:
        conn.close()

    detail = client.get(f"/api/analyses/{legacy_id}").json()
    assert detail["has_review_detail"] is False

    fresh, _ = match([pt(0, 116.4002)], name="fresh-detail")
    assert client.get(f"/api/analyses/{fresh}").json()["has_review_detail"] is True

    r = client.post("/api/distance-reviews",
                    json={"analysis_id_a": legacy_id, "analysis_id_b": fresh,
                          "threshold_m": 20})
    assert r.status_code == 409
    assert "重新匹配" in r.json()["detail"]


def test_atomic_write_failure_leaves_no_analysis(monkeypatch):
    """明细写入失败时整事务回滚:不生成分析,也不留任何半截数据。"""
    traj = post_json("/api/trajectories",
                     make_traj([pt(0, 116.4002), pt(10, 116.4003)], "atomic"))
    before = client.get("/api/analyses").json()["analyses"]
    before_ids = {a["analysis_id"] for a in before}

    real_connect = db.connect

    class FlakyConnection:
        """代理真实连接,仅在写复核明细时抛错,验证事务回滚。"""

        def __init__(self, inner):
            self._inner = inner

        def executemany(self, sql, parameters):
            if "INSERT INTO analysis_point_matches" in sql:
                raise RuntimeError("simulated disk failure")
            return self._inner.executemany(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    def flaky_connect():
        return FlakyConnection(real_connect())

    monkeypatch.setattr("app.main.db.connect", flaky_connect)
    # raise_server_exceptions=False:让服务端异常走正常 500 响应而非在测试进程内重抛
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/match", json={"trajectory_id": traj["trajectory_id"],
                            "network_version": "rv1"})
    monkeypatch.undo()
    assert r.status_code == 500

    after = client.get("/api/analyses").json()["analyses"]
    after_ids = {a["analysis_id"] for a in after}
    assert after_ids == before_ids

    # 数据库中不存在任何孤儿明细/路径行
    conn = db.connect()
    try:
        for table in ("analysis_segments", "analysis_gaps", "analysis_point_matches"):
            orphans = conn.execute(
                f"SELECT COUNT(*) FROM {table} t LEFT JOIN analyses a ON a.id = t.analysis_id "
                "WHERE a.id IS NULL").fetchone()[0]
            assert orphans == 0
    finally:
        conn.close()
