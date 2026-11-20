import hashlib, secrets, sqlite3, uuid
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 泄漏修复册已封存口径：甲（旁路暂存）。乙（开始瞬间拒收）不得与甲混用。
LEAK_POLICY = "甲"

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def now_str() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

DEFAULT_CUTOVER_MAPPINGS = [
    ("OLD-A1", "NEW-100", "一对一"),
    ("OLD-B1", "NEW-200", "多对一"),
    ("OLD-B2", "NEW-200", "多对一"),
    ("OLD-D1", "NEW-300", "一对一"),
]

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cutovers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          from_version TEXT NOT NULL,
          to_version TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          closed_at TEXT,
          receipt_no TEXT,
          released_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS staging(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cutover_id INTEGER NOT NULL,
          old_key TEXT NOT NULL,
          target_key TEXT,
          version TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          released_at TEXT
        );
        CREATE TABLE IF NOT EXISTS rejections(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          cutover_id INTEGER,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS window_locks(
          id INTEGER PRIMARY KEY CHECK (id = 1),
          holder TEXT,
          acquired_at TEXT
        );
        CREATE TABLE IF NOT EXISTS lock_contention(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          route TEXT NOT NULL,
          arriving TEXT NOT NULL,
          blocked_by TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leak_deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        conn.execute("INSERT OR IGNORE INTO window_locks(id, holder, acquired_at) VALUES (1, NULL, NULL)")
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))

@app.on_event("startup")
def _s(): init_db()

# ---------------------------------------------------------------- 窗口锁

def try_acquire_window_lock(conn, holder: str) -> bool:
    """原子抢窗口锁。抢到返回 True；已被占用返回 False（后到只记争用）。"""
    cur = conn.execute(
        "UPDATE window_locks SET holder=?, acquired_at=? WHERE id=1 AND holder IS NULL",
        (holder, now_str()),
    )
    conn.commit()
    return cur.rowcount == 1

def release_window_lock(conn, holder: str):
    conn.execute("UPDATE window_locks SET holder=NULL, acquired_at=NULL WHERE id=1 AND holder=?", (holder,))
    conn.commit()

def lock_holder(conn) -> str | None:
    r = conn.execute("SELECT holder FROM window_locks WHERE id=1").fetchone()
    return r["holder"] if r else None

def record_contention(conn, route: str, arriving: str):
    blocked_by = lock_holder(conn)
    conn.execute(
        "INSERT INTO lock_contention(route,arriving,blocked_by,created_at) VALUES (?,?,?,?)",
        (route, arriving, blocked_by, now_str()),
    )
    conn.commit()

# ---------------------------------------------------------------- 口径守卫

def enforce_policy(policy: str | None):
    """偏离已封存口径（甲）的写路径一律拒写，并记「泄漏偏了」。"""
    p = policy or LEAK_POLICY
    if p != LEAK_POLICY:
        with connect() as c:
            c.execute("INSERT INTO leak_deviations(detail,created_at) VALUES (?,?)",
                      (f"请求口径={p}，与封存口径={LEAK_POLICY}不符，拒写", now_str()))
        raise HTTPException(409, "偏离修复册，拒写并已记泄漏偏了")

# ---------------------------------------------------------------- 查询

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH), "leak_policy": LEAK_POLICY}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}

@app.get("/api/staging")
def staging(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM staging ORDER BY id DESC").fetchall()]}

@app.get("/api/rejections")
def rejections(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM rejections ORDER BY id DESC").fetchall()]}

@app.get("/api/cutovers")
def cutovers(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM cutovers ORDER BY id DESC").fetchall()],
                "active": dict(c.execute("SELECT * FROM cutovers WHERE status='切版中' ORDER BY id DESC LIMIT 1").fetchone() or {})}

@app.get("/api/lock-contention")
def contention(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM lock_contention ORDER BY id DESC").fetchall()],
                "holder": lock_holder(c)}

@app.get("/api/leak-deviations")
def leak_deviations(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM leak_deviations ORDER BY id DESC").fetchall()]}

def _active_cutover(conn):
    return conn.execute("SELECT * FROM cutovers WHERE status='切版中' ORDER BY id DESC LIMIT 1").fetchone()

def _effective_version(conn):
    return conn.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()

# ---------------------------------------------------------------- 切版台

class CutoverIn(BaseModel):
    to_version: str | None = None
    policy: str | None = None

@app.post("/api/cutover/start")
def cutover_start(body: CutoverIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    enforce_policy(body.policy)
    started = now_str()
    holder = "cutover:start:" + uuid.uuid4().hex[:8]
    with connect() as c:
        if not try_acquire_window_lock(c, holder):
            record_contention(c, "cutover", holder)
            raise HTTPException(423, "窗口锁被占用，只记争用")
        try:
            if _active_cutover(c):
                raise HTTPException(409, "切版已在进行，禁止重入")
            eff = _effective_version(c)
            if not eff:
                raise HTTPException(404, "无生效映射版本")
            to_version = body.to_version or "m-2026.12"
            if c.execute("SELECT 1 FROM map_versions WHERE version=?", (to_version,)).fetchone():
                raise HTTPException(409, "目标版本已存在")
            c.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)",
                      (to_version, "切版中", started))
            c.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)",
                          [(to_version, o, n, k) for (o, n, k) in DEFAULT_CUTOVER_MAPPINGS])
            c.execute("INSERT INTO cutovers(from_version,to_version,status,started_at) VALUES (?,?,?,?)",
                      (eff["version"], to_version, "切版中", started))
            cid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            release_window_lock(c, holder)
        return {"cutover_id": cid, "from_version": eff["version"], "to_version": to_version, "status": "切版中"}

@app.post("/api/cutover/close")
def cutover_close(authorization: str | None = Header(default=None)):
    require_user(authorization)
    holder = "cutover:close:" + uuid.uuid4().hex[:8]
    with connect() as c:
        if not try_acquire_window_lock(c, holder):
            record_contention(c, "cutover", holder)
            raise HTTPException(423, "窗口锁被占用，只记争用")
        try:
            active = _active_cutover(c)
            if not active:
                raise HTTPException(409, "无进行中的切版")
            rows = c.execute("SELECT * FROM staging WHERE cutover_id=? AND status='待放行' ORDER BY id",
                             (active["id"],)).fetchall()
            released = 0
            stamp = now_str()
            for row in rows:
                m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                              (active["to_version"], row["old_key"])).fetchone()
                if not m:
                    raise HTTPException(409, f"暂存行 {row['old_key']} 在目标版本无映射")
                c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                          (m["new_key"], row["old_key"], active["to_version"], stamp))
                c.execute("UPDATE staging SET status='已放行', target_key=?, released_at=? WHERE id=?",
                          (m["new_key"], stamp, row["id"]))
                released += 1
            c.execute("UPDATE map_versions SET status='已收口' WHERE status='生效' AND version<>?",
                      (active["to_version"],))
            c.execute("UPDATE map_versions SET status='生效' WHERE version=?", (active["to_version"],))
            receipt = "RCPT-" + uuid.uuid4().hex[:10].upper()
            c.execute("UPDATE cutovers SET status='已收口', closed_at=?, receipt_no=?, released_count=? WHERE id=?",
                      (stamp, receipt, released, active["id"]))
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            release_window_lock(c, holder)
        return {"receipt_no": receipt, "released_count": released, "to_version": active["to_version"]}

# ---------------------------------------------------------------- 入站装载

class LoadIn(BaseModel):
    old_key: str
    policy: str | None = None

def _ingest(old_key: str, policy: str | None):
    """入站写入路径（泄漏修复册 · 甲：旁路暂存）。

    切版未收口时：新到入站只写 hold 暂存（待放行），正式实体零增；
    暂存与切版态同事务，失败一并回滚。切版与装载并发占窗口锁，
    后到只记争用、不写正式。
    """
    enforce_policy(policy)
    stamp = now_str()
    holder = "load:" + uuid.uuid4().hex[:8]
    with connect() as c:
        if not try_acquire_window_lock(c, holder):
            record_contention(c, "load", holder)
            raise HTTPException(423, "窗口锁被占用，只记争用")
        try:
            active = _active_cutover(c)
            if active:
                version = active["to_version"]
                m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                              (version, old_key)).fetchone()
                # 甲：窗口内新到入站一律只写 hold 暂存（无目标映射也暂存，target 留空），
                # 正式零增；收口放行时若无映射，暂存与切版态同事务回滚。
                cur = c.execute(
                    "INSERT INTO staging(cutover_id,old_key,target_key,version,status,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (active["id"], old_key, (m["new_key"] if m else None), version, "待放行", stamp),
                )
                staging_id = cur.lastrowid
                c.commit()
                return {"status": "待放行", "staged": True, "staging_id": staging_id,
                        "entity_key": None, "version": version,
                        "target_key": (m["new_key"] if m else None),
                        "formal_unchanged": True}
            eff = _effective_version(c)
            if not eff:
                raise HTTPException(404, "无生效映射版本")
            version = eff["version"]
            m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                          (version, old_key)).fetchone()
            if not m:
                c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)",
                          (old_key, "无映射", "挂起"))
                c.commit()
                raise HTTPException(409, "无映射，已进冲突队列")
            c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                      (m["new_key"], old_key, version, stamp))
            c.commit()
            return {"entity_key": m["new_key"], "version": version, "staged": False}
        except HTTPException:
            c.rollback()
            raise
        except Exception:
            c.rollback()
            raise
        finally:
            release_window_lock(c, holder)

@app.post("/api/load")
def load(body: LoadIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    return _ingest(body.old_key, body.policy)

class RemapIn(BaseModel):
    old_key: str
    policy: str | None = None

@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    return _ingest(body.old_key, body.policy)

# ---------------------------------------------------------------- 互证页 / 对账

def _attestation(c) -> dict:
    active = _active_cutover(c)
    staged = c.execute("SELECT COUNT(*) n FROM staging WHERE status='待放行'").fetchone()["n"]
    released = c.execute("SELECT COUNT(*) n FROM staging WHERE status='已放行'").fetchone()["n"]
    formal_rows = c.execute("SELECT * FROM formal_entities").fetchall()
    half_baked = c.execute("""SELECT COUNT(*) n FROM formal_entities f
                              JOIN map_versions v ON v.version=f.version
                              WHERE v.status='切版中'""").fetchone()["n"]
    rej = c.execute("SELECT COUNT(*) n FROM rejections").fetchone()["n"]
    receipt = active  # 切版未收口时不应有完成回执
    last_closed = c.execute("SELECT * FROM cutovers WHERE status='已收口' ORDER BY id DESC LIMIT 1").fetchone()
    ledger = 0
    if last_closed:
        ledger = c.execute("SELECT COUNT(*) n FROM formal_entities WHERE version=?",
                           (last_closed["to_version"],)).fetchone()["n"]
    # 同态判定：暂存区 / 正式实体 / 切版台 / 拒收凭证 / 互证页五处同态
    checks = {
        "formal_no_half_baked": half_baked == 0,
        "window_no_leak": True,
        # 甲乙互斥：封存口径为甲时，拒收凭证必须为空；出现即乙被混入
        "policy_exclusive_no_rejections": (rej == 0) if LEAK_POLICY == "甲" else True,
    }
    if active:
        leaked = c.execute("SELECT COUNT(*) n FROM formal_entities WHERE version=?",
                           (active["to_version"],)).fetchone()["n"]
        checks["window_no_leak"] = leaked == 0
    if last_closed:
        checks["receipt_matches_ledger"] = (
            last_closed["released_count"] == ledger == released
        )
    else:
        checks["receipt_matches_ledger"] = True
    return {
        "active_cutover": dict(active) if active else None,
        "staging": {"待放行": staged, "已放行": released},
        "formal": {"rows": len(formal_rows), "half_baked": half_baked},
        "cutover": {"active": bool(active), "last_closed": dict(last_closed) if last_closed else None},
        "rejections": rej,
        "checks": checks,
        "consistent": all(checks.values()),
    }

@app.get("/api/attestation")
def attestation(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return _attestation(c)

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        last = c.execute("SELECT * FROM cutovers WHERE status='已收口' ORDER BY id DESC LIMIT 1").fetchone()
        if not last:
            return {"receipt_no": None, "receipt_count": 0, "ledger_count": 0, "staging_released": 0, "consistent": True}
        ledger = c.execute("SELECT COUNT(*) n FROM formal_entities WHERE version=?", (last["to_version"],)).fetchone()["n"]
        released = c.execute("SELECT COUNT(*) n FROM staging WHERE cutover_id=? AND status='已放行'", (last["id"],)).fetchone()["n"]
        return {
            "receipt_no": last["receipt_no"],
            "receipt_count": last["released_count"],
            "ledger_count": ledger,
            "staging_released": released,
            "consistent": last["released_count"] == ledger == released,
        }

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
