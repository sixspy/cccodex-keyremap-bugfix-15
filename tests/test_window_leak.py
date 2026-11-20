"""窗口泄漏修复红测（泄漏修复册 · 甲：旁路暂存）。

本文件在改分流写路径之前先落，必须见红。
"""
import pytest
from fastapi.testclient import TestClient
from app import app, init_db, connect


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c):
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_cutover_window_does_not_leak_into_formal():
    """复现：切版未收口却正式已进新键。

    甲路径要求：窗口内新到入站只写入 hold 暂存（待放行），
    正式实体保持零增，暂存可查，五处同态。
    """
    c = TestClient(app)
    h = _auth(c)

    r = c.post("/api/cutover/start", headers=h, json={"to_version": "m-2026.12"})
    assert r.status_code == 200, r.text
    active = c.get("/api/cutovers", headers=h).json()["active"]
    assert active and active["status"] == "切版中"

    before = c.get("/api/formal", headers=h).json()["items"]
    before_ids = {x["id"] for x in before}

    # 切版窗口内新到入站
    r = c.post("/api/load", headers=h, json={"old_key": "OLD-B1"})
    assert r.status_code == 200, r.text

    after = c.get("/api/formal", headers=h).json()["items"]
    new_rows = [x for x in after if x["id"] not in before_ids]
    assert new_rows == [], f"切版未收口时正式区不得新增，实际新增 {new_rows}"

    assert r.json().get("staged") is True, "窗口内入站必须只进暂存"

    # 正式区不得出现半成品（切版中版本）挂版
    assert not any(x["version"] == "m-2026.12" for x in after)

    # 暂存区可查
    st = c.get("/api/staging", headers=h).json()["items"]
    assert any(x["old_key"] == "OLD-B1" and x["status"] == "待放行" for x in st), st

    # 互证页须同态
    att = c.get("/api/attestation", headers=h).json()
    assert att["consistent"] is True, att


def test_window_lock_contention_on_load_records_only():
    """抢装载争用：窗口锁已被切版占用时，后到装载只记争用、不得写正式。

    修复前装载不占窗口锁，会直接写正式且无争用记录 → 见红。
    """
    c = TestClient(app)
    h = _auth(c)

    # 另一路（切版）已持窗口锁
    with connect() as conn:
        conn.execute(
            "UPDATE window_locks SET holder=?, acquired_at=datetime('now') WHERE id=1",
            ("cutover:test",),
        )
        conn.commit()

    r = c.post("/api/load", headers=h, json={"old_key": "OLD-B1"})
    assert r.status_code == 423, f"后到装载应被窗口锁挡下，实际 {r.status_code} {r.text}"

    con = c.get("/api/lock-contention", headers=h).json()
    assert any(x["route"] == "load" for x in con["items"]), "后到装载必须记争用"

    formal = c.get("/api/formal", headers=h).json()["items"]
    assert not any(x["mapped_from"] == "OLD-B1" for x in formal), "争用路径不得写正式"


def test_staging_released_on_close_receipt_matches_ledger():
    """甲路径闭环：收口时同一事务放行暂存，完成回执与挂版账对齐，五处同态。"""
    c = TestClient(app)
    h = _auth(c)

    assert c.post("/api/cutover/start", headers=h, json={"to_version": "m-2026.12"}).status_code == 200
    before = {x["id"] for x in c.get("/api/formal", headers=h).json()["items"]}

    for key in ("OLD-B1", "OLD-B2", "OLD-D1"):
        r = c.post("/api/load", headers=h, json={"old_key": key})
        assert r.status_code == 200 and r.json()["staged"] is True, r.text

    # 窗口内正式零增
    assert {x["id"] for x in c.get("/api/formal", headers=h).json()["items"]} == before

    r = c.post("/api/cutover/close", headers=h)
    assert r.status_code == 200, r.text
    closed = r.json()
    assert closed["released_count"] == 3

    formal_new = [x for x in c.get("/api/formal", headers=h).json()["items"] if x["id"] not in before]
    assert len(formal_new) == 3
    assert all(x["version"] == "m-2026.12" for x in formal_new)

    stag = c.get("/api/staging", headers=h).json()["items"]
    assert len([x for x in stag if x["status"] == "已放行"]) == 3

    rec = c.get("/api/reconcile", headers=h).json()
    assert rec["consistent"] is True, rec
    assert rec["receipt_count"] == rec["ledger_count"] == rec["staging_released"] == 3

    att = c.get("/api/attestation", headers=h).json()
    assert att["consistent"] is True, att
    assert att["formal"]["half_baked"] == 0


def test_release_failure_rolls_back_staging_and_cutover():
    """放行失败：暂存与切版态同事务回滚，正式零增，切版仍处切版中。"""
    c = TestClient(app)
    h = _auth(c)

    assert c.post("/api/cutover/start", headers=h, json={"to_version": "m-2026.12"}).status_code == 200
    active = c.get("/api/cutovers", headers=h).json()["active"]
    before = {x["id"] for x in c.get("/api/formal", headers=h).json()["items"]}

    # 直接植入一条目标版本无映射的暂存行，模拟放行期失败
    with connect() as conn:
        conn.execute(
            "INSERT INTO staging(cutover_id,old_key,target_key,version,status,created_at) "
            "VALUES (?,?,?,?,?,datetime('now'))",
            (active["id"], "OLD-NOMAP", "NEW-999", "m-2026.12", "待放行"),
        )
        conn.commit()

    r = c.post("/api/cutover/close", headers=h)
    assert r.status_code == 409, r.text

    # 切版态未变
    still = c.get("/api/cutovers", headers=h).json()["active"]
    assert still and still["id"] == active["id"] and still["status"] == "切版中", still
    # 暂存行仍待放行
    stag = c.get("/api/staging", headers=h).json()["items"]
    assert any(x["old_key"] == "OLD-NOMAP" and x["status"] == "待放行" for x in stag)
    # 正式零增
    assert {x["id"] for x in c.get("/api/formal", headers=h).json()["items"]} == before


def test_deviating_policy_is_refused_and_recorded():
    """偏离封存口径（甲）的写路径拒写，并记「泄漏偏了」。"""
    c = TestClient(app)
    h = _auth(c)

    assert c.post("/api/cutover/start", headers=h, json={"to_version": "m-2026.12"}).status_code == 200
    r = c.post("/api/load", headers=h, json={"old_key": "OLD-B1", "policy": "乙"})
    assert r.status_code == 409, r.text

    dev = c.get("/api/leak-deviations", headers=h).json()["items"]
    assert any("乙" in x["detail"] for x in dev)
    # 拒写后正式仍零增
    assert not any(x["mapped_from"] == "OLD-B1" for x in c.get("/api/formal", headers=h).json()["items"])
