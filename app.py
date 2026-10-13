"""
خادم موقع «عهدة الأقسام والقاعات»
FastAPI + SQLite — يحفظ الأقسام والقاعات والأصناف، ويقدّم الواجهة (index.html).

التشغيل:
    pip install -r requirements.txt
    python app.py                    # ثم افتح http://localhost:8000

متغيرات اختيارية:
    INV_DB      مسار ملف قاعدة البيانات (الافتراضي: data/inventory.db)
    INV_HOST    عنوان الاستماع (الافتراضي: 0.0.0.0 ليصل له من بقية أجهزة الشبكة)
    INV_PORT    المنفذ (الافتراضي: 8000)
"""
import base64
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("INV_DB", ROOT / "data" / "inventory.db"))
SEED_PATH = ROOT / "seed.json"
COLLECTIONS = {"depts", "rooms", "items"}
ID_RE = re.compile(r"^[A-Za-z0-9_.~:@+\-]{1,200}$")

_lock = threading.Lock()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS docs(
               col TEXT NOT NULL,
               id  TEXT NOT NULL,
               data TEXT NOT NULL,
               updated REAL NOT NULL,
               PRIMARY KEY(col, id))"""
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return con


con = connect()


def bump_version():
    v = str(time.time_ns())
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('version', ?)", (v,))
    return v


def current_version():
    row = con.execute("SELECT v FROM meta WHERE k='version'").fetchone()
    return row[0] if row else "0"


def seed_if_empty():
    """أول تشغيل: يحمّل البيانات الحالية من data/seed.json."""
    with _lock:
        (count,) = con.execute("SELECT COUNT(*) FROM docs").fetchone()
        if count or not SEED_PATH.exists():
            return
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        now = time.time()
        for col in COLLECTIONS:
            for doc_id, data in (seed.get(col) or {}).items():
                con.execute(
                    "INSERT OR REPLACE INTO docs(col, id, data, updated) VALUES(?,?,?,?)",
                    (col, doc_id, json.dumps(data, ensure_ascii=False), now),
                )
        bump_version()
        con.commit()


seed_if_empty()

app = FastAPI(title="عهدة الأقسام والقاعات", docs_url=None, redoc_url=None, openapi_url=None)

# كلمة المرور: تُفعَّل إذا ضبطت المتغير INV_PASSWORD (واسم المستخدم INV_USER، الافتراضي admin)
AUTH_USER = os.environ.get("INV_USER", "admin")
AUTH_PASS = os.environ.get("INV_PASSWORD", "")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if AUTH_PASS:
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS)
            except Exception:
                ok = False
        if not ok:
            return Response("الدخول يحتاج اسم مستخدم وكلمة مرور", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="inventory", charset="UTF-8"'},
                            media_type="text/plain; charset=utf-8")
    return await call_next(request)


def check(col: str, doc_id: str | None = None):
    if col not in COLLECTIONS:
        raise HTTPException(404, "collection not found")
    if doc_id is not None and not ID_RE.match(doc_id):
        raise HTTPException(400, "bad id")


@app.get("/api/ping")
def ping():
    return {"ok": True}


@app.get("/api/version")
def version():
    return {"version": current_version()}


@app.get("/api/all")
def all_docs():
    out = {c: {} for c in COLLECTIONS}
    with _lock:
        for col, doc_id, data in con.execute("SELECT col, id, data FROM docs"):
            out[col][doc_id] = json.loads(data)
        out["version"] = current_version()
    return JSONResponse(out)


@app.put("/api/{col}/{doc_id}")
def put_doc(col: str, doc_id: str, data: dict = Body(...)):
    check(col, doc_id)
    payload = json.dumps(data, ensure_ascii=False)
    if len(payload.encode()) > 256 * 1024:
        raise HTTPException(413, "document too large")
    with _lock:
        con.execute(
            "INSERT OR REPLACE INTO docs(col, id, data, updated) VALUES(?,?,?,?)",
            (col, doc_id, payload, time.time()),
        )
        v = bump_version()
        con.commit()
    return {"ok": True, "version": v}


@app.delete("/api/{col}/{doc_id}")
def delete_doc(col: str, doc_id: str):
    check(col, doc_id)
    with _lock:
        con.execute("DELETE FROM docs WHERE col=? AND id=?", (col, doc_id))
        v = bump_version()
        con.commit()
    return {"ok": True, "version": v}


@app.get("/api/export")
def export_all():
    """نسخة احتياطية JSON لكل البيانات (تُسترد من صفحة Excel والنسخ الاحتياطي)."""
    data = all_docs().body
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="inventory-backup.json"'},
    )


if (ROOT / "static").is_dir():
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html", media_type="text/html; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("INV_HOST", "0.0.0.0")
    port = int(os.environ.get("INV_PORT") or os.environ.get("PORT") or "8000")
    print(f"افتح المتصفح على: http://localhost:{port}")
    uvicorn.run(app, host=host, port=port)
