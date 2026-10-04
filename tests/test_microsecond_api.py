"""HTTP API 集成测试：带微秒采样时刻的单窗审定与跨窗复核。

覆盖：
- 单窗审定按原始半开区段的实际时长判定（不再先舍入到毫秒）：
  持续 1.0004s 超限对阈值 1.0002s 判不合格；恰好等于阈值不判不合格；
- 任意正时长未知区段（0.0004s 边界空白）判待定；
- 输出时长呈现影响结论的微秒差异（duration_seconds /
  max_continuous_exceeded_seconds / unknown_seconds）；
- 跨窗复核同一口径：不足 0.0005s 的未知时段不再舍入为零；
- 微秒数据下后续签发、封存、解封与核验包出具/核验仍按现有流程工作；
- 非法输入拒绝语义不变（缺时区 422 等）。

运行：PYTHONPATH=. python -m pytest tests/test_microsecond_api.py -q
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_window(client, start, end, *, max_exceed=300.0, max_gap=600.0,
                tmin=18.0, tmax=22.0, name=None):
    body = {
        "name": name or f"窗口-{start}",
        "start_at": start,
        "end_at": end,
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def rd(client, wid, rid, measured_at, temp):
    r = client.post(f"/api/v1/windows/{wid}/readings", json={
        "reading_id": rid, "revision": 1,
        "measured_at": measured_at, "temperature": temp})
    assert r.status_code == 201, r.text
    return r


def issue(client, wid, note=None):
    r = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                    json={"note": note} if note is not None else {})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- 单窗审定（微秒）

def test_single_window_microsecond_exceed_fails(client):
    """持续 1.0004s 超限对阈值 1.0002s：按实际时长严格超过 -> 不合格。"""
    wid = make_window(client, "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1.0002, max_gap=10)
    rd(client, wid, "a", "2026-10-01T00:00:00Z", 23.0)
    rd(client, wid, "b", "2026-10-01T00:00:01.000400Z", 20.0)
    rd(client, wid, "c", "2026-10-01T00:00:02Z", 20.0)

    r = client.get(f"/api/v1/windows/{wid}/evaluation")
    assert r.status_code == 200
    ev = r.json()
    assert ev["verdict"] == "fail"
    assert ev["max_continuous_exceeded_seconds"] == 1.0004
    assert ev["max_exceed_seconds"] == 1.0002
    # 输出时长呈现微秒差异
    assert ev["exceeded_segments"][0]["duration_seconds"] == 1.0004
    assert ev["exceeded_segments"][0]["start"] == "2026-10-01T00:00:00Z"
    assert ev["exceeded_segments"][0]["end"] == "2026-10-01T00:00:01.000400Z"
    assert ev["normal_segments"][0]["duration_seconds"] == 0.9996


def test_single_window_microsecond_exactly_at_threshold_not_fail(client):
    """持续恰好 1.0002s == 阈值：不严格超过 -> 不判不合格。"""
    wid = make_window(client, "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1.0002, max_gap=10)
    rd(client, wid, "a", "2026-10-01T00:00:00Z", 23.0)
    rd(client, wid, "b", "2026-10-01T00:00:01.000200Z", 20.0)
    rd(client, wid, "c", "2026-10-01T00:00:02Z", 20.0)

    ev = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "pass"
    assert ev["max_continuous_exceeded_seconds"] == 1.0002


def test_single_window_tiny_unknown_pending(client):
    """0.0004s 边界空白未知（不足 0.0005s）：任意正时长未知 -> 待定。"""
    wid = make_window(client, "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1.0002, max_gap=10)
    rd(client, wid, "a", "2026-10-01T00:00:00.000400Z", 20.0)
    rd(client, wid, "b", "2026-10-01T00:00:02Z", 20.0)

    ev = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "pending"
    assert ev["has_unknown"] is True
    assert ev["unknown_segments"][0]["duration_seconds"] == 0.0004
    assert ev["unknown_segments"][0]["end"] == "2026-10-01T00:00:00.000400Z"


def test_microsecond_reading_naive_timestamp_still_rejected(client):
    """非法输入拒绝语义不变：微秒时刻也必须带时区。"""
    wid = make_window(client, "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1.0002, max_gap=10)
    r = client.post(f"/api/v1/windows/{wid}/readings", json={
        "reading_id": "a", "revision": 1,
        "measured_at": "2026-10-01T00:00:00.000400", "temperature": 20.0})
    assert r.status_code == 422


# ---------------------------------------------------------------- 证书/封存/核验（微秒数据，流程不变）

def test_microsecond_issue_seal_and_verify_flow(client):
    """微秒数据下：签发固化微秒精度结果；封存、解封、核验包流程照常。"""
    wid = make_window(client, "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                      max_exceed=1.0002, max_gap=10)
    rd(client, wid, "a", "2026-10-01T00:00:00Z", 23.0)
    rd(client, wid, "b", "2026-10-01T00:00:01.000400Z", 20.0)
    rd(client, wid, "c", "2026-10-01T00:00:02Z", 20.0)

    # 签发：不可变快照固化微秒精度审定结果
    cert = issue(client, wid, "微秒采样签发")
    assert cert["verdict"] == "fail"
    snap_ev = cert["snapshot"]["evaluation"]
    assert snap_ev["max_continuous_exceeded_seconds"] == 1.0004
    assert snap_ev["exceeded_segments"][0]["duration_seconds"] == 1.0004
    # 快照读数保留微秒时刻
    assert cert["snapshot"]["readings"][1]["measured_at"] == "2026-10-01T00:00:01.000400Z"

    # 封存：快照与当前状态一致（同一微秒口径）-> 成功
    r = client.post(f"/api/v1/windows/{wid}/seals",
                    json={"certificate_id": cert["id"], "reason": "微秒窗口封存"})
    assert r.status_code == 201, r.text
    seal = r.json()["seal"]
    assert seal["status"] == "sealed"
    assert seal["evidence"]["snapshot"]["evaluation"][
        "max_continuous_exceeded_seconds"] == 1.0004

    # 封存期间签发被拒绝（现有流程不变）；解封后恢复
    assert client.post(f"/api/v1/windows/{wid}/certificates/issue",
                       json={}).status_code == 409
    r = client.post(f"/api/v1/window-seals/{seal['id']}/unseal",
                    json={"reason": "解封演示"})
    assert r.status_code == 201 and r.json()["effect"] == "unsealed"

    # 核验包出具与核验照常（证书未撤销，当前可用）
    r = client.post(f"/api/v1/certificates/{cert['id']}/verification-package")
    assert r.status_code == 201, r.text
    package = r.json()
    r = client.post("/api/v1/verification-packages/verify", json=package)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["signature_check"]["trusted"] is True
    assert d["snapshot_check"]["trusted"] is True
    assert d["certificate"]["state"] == "current"
    assert d["certificate"]["verdict"] == "fail"


# ---------------------------------------------------------------- 跨窗复核（微秒）

def _make_us_chain(client):
    """两个首尾相接的微秒窗口：[0,2s) 与 [2s,4s)，返回 (w1, w2, c1, c2)。"""
    w1 = make_window(client, "2026-10-01T00:00:00Z", "2026-10-01T00:00:02Z",
                     max_exceed=300, max_gap=10, name="微秒窗-1")
    w2 = make_window(client, "2026-10-01T00:00:02Z", "2026-10-01T00:00:04Z",
                     max_exceed=300, max_gap=10, name="微秒窗-2")
    return w1, w2


def test_cross_window_microsecond_exceed_fails(client):
    """跨窗复核：持续 1.0004s 超限对全程上限 1.0002s -> 不合格。"""
    w1, w2 = _make_us_chain(client)
    rd(client, w1, "a", "2026-10-01T00:00:00Z", 24.0)
    rd(client, w1, "b", "2026-10-01T00:00:01.000400Z", 20.0)
    rd(client, w1, "c", "2026-10-01T00:00:02Z", 20.0)
    rd(client, w2, "d", "2026-10-01T00:00:02Z", 20.0)
    rd(client, w2, "e", "2026-10-01T00:00:04Z", 20.0)
    c1, c2 = issue(client, w1), issue(client, w2)

    r = client.post("/api/v1/cross-window-reviews", json={
        "certificate_ids": [c1["id"], c2["id"]],
        "max_continuous_exceed_seconds": 1.0002})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["verdict"] == "fail"
    assert d["max_continuous_exceeded_seconds"] == 1.0004
    assert d["exceeded_seconds"] == 1.0004
    exc = [s for s in d["segments"] if s["status"] == "exceeded"]
    assert len(exc) == 1 and exc[0]["duration_seconds"] == 1.0004

    # 恰好等于上限（1.0004）不算严格超过 -> 合格
    r = client.post("/api/v1/cross-window-reviews", json={
        "certificate_ids": [c1["id"], c2["id"]],
        "max_continuous_exceed_seconds": 1.0004})
    assert r.status_code == 201
    assert r.json()["verdict"] == "pass"


def test_cross_window_tiny_unknown_pending(client):
    """跨窗复核：0.0004s 未知时段不再舍入为零 -> 待定。"""
    w1, w2 = _make_us_chain(client)
    rd(client, w1, "a", "2026-10-01T00:00:00Z", 20.0)
    rd(client, w1, "b", "2026-10-01T00:00:02Z", 20.0)
    # 窗 2 首读在起点后 0.0004s -> [00:00:02, 00:00:02.000400) 边界空白未知
    rd(client, w2, "d", "2026-10-01T00:00:02.000400Z", 20.0)
    rd(client, w2, "e", "2026-10-01T00:00:04Z", 20.0)
    c1, c2 = issue(client, w1), issue(client, w2)

    r = client.post("/api/v1/cross-window-reviews", json={
        "certificate_ids": [c1["id"], c2["id"]],
        "max_continuous_exceed_seconds": 1.0002})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["verdict"] == "pending"
    assert d["unknown_seconds"] == 0.0004
    assert d["has_unknown"] is True
    unk = [s for s in d["segments"] if s["status"] == "unknown"]
    assert len(unk) == 1 and unk[0]["duration_seconds"] == 0.0004
    assert unk[0]["source"]["certificate_id"] == c2["id"]
    assert d["sources"][1]["unknown_seconds"] == 0.0004
    # 区段时长合计守恒（微秒精度）
    assert sum(s["duration_seconds"] for s in d["segments"]) == d["total_seconds"] == 4.0
