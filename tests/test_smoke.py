
import pytest
from fastapi.testclient import TestClient
from app import app, init_db

@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path/"t.db"); init_db(); yield

def test_remap_and_conflict():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "实体键桥"
    h = {"Authorization":"Bearer "+c.post("/api/login", json={"username":"admin","password":"123456"}).json()["token"]}
    assert c.post("/api/remap", headers=h, json={"old_key":"OLD-B2"}).json()["entity_key"] == "NEW-200"
    assert c.post("/api/remap", headers=h, json={"old_key":"OLD-ZZ"}).status_code == 409
    assert any(x["old_key"]=="OLD-ZZ" for x in c.get("/api/conflicts", headers=h).json()["items"])
