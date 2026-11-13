#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池状态存储（pool_store）：内置 SQLite 作为唯一事实源。

为什么要有它
------------
2026-10-17 复盘：号池状态散落在 `pool-config.json`、`.pool-reconciler-state.json`、
`admin-audit.jsonl`、`pool-affinity.json`、`dsh-instance-keys.json` 与 `.env` 里，
靠各自的读-改-写 + 缓存同步；一次「手工改 config.yaml 被 cron 覆盖」就足以让状态与
线上不一致，排查时又无法回答"上一次 rotate 是谁、什么时候、改成了什么"。

本模块把这些文档收进**一个内嵌 SQLite 文件**（`${POOL_HOME}/pool.db`，WAL，0600）：
- 事务 + `BEGIN IMMEDIATE` 串行化写入，跨进程安全（配 `busy_timeout`）；
- 文档表保留原 JSON 形状（`documents`），另把账号/部署展开成关系表（`accounts`/`deployments`）
  便于看板直接查询，不必再解析 config.yaml；
- 每次写入仍**镜像回原 JSON 文件**，老的读数方（sidecar / 看板 / 运维脚本）行为不变。

设计约束
--------
- 零依赖：只用标准库 `sqlite3`；
- 失败必须降级而不是打断：任何 SQLite 异常都由调用方回退到原文件路径（见 pool_control）；
- schema 版本写在 `meta.schema_version`，升级用 `_migrate()` 逐版本推进。
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 1
DB_NAME = "pool.db"
STORE_ENV = "POOL_STORE"          # sqlite（默认）| json
AUDIT_MAX = 20000                 # 审计留存量（超出按时间裁剪）

_INIT_LOCK = threading.Lock()
_READY = {}                       # path → (schema_version, mtime)


def pool_home():
    return os.environ.get("POOL_HOME") or "/opt/litellm"


def db_path():
    return os.environ.get("POOL_DB") or os.path.join(pool_home(), DB_NAME)


def enabled():
    """POOL_STORE=json 可整体退回旧的文件事实源（应急开关）。"""
    return (os.environ.get(STORE_ENV) or "sqlite").strip().lower() != "json"


def _connect(path=None):
    p = path or db_path()
    _ensure_private_file(p)
    conn = sqlite3.connect(p, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn, p)
    return conn


def _ensure_private_file(path):
    """库文件仅 root 可读写；新建即收紧，已存在也纠正一次。"""
    if os.name == "nt":
        return
    try:
        if not os.path.exists(path):
            open(path, "a").close()
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_schema(conn, path):
    with _INIT_LOCK:
        conn.execute("""CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        cur = int(row["value"]) if row else 0
        if cur < SCHEMA_VERSION:
            _migrate(conn, cur)
        conn.execute("""CREATE TABLE IF NOT EXISTS documents(
            name TEXT PRIMARY KEY, doc TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS accounts(
            slot INTEGER PRIMARY KEY, key_var TEXT, email TEXT, enabled INTEGER,
            key_fp TEXT, updated_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS deployments(
            identity TEXT PRIMARY KEY, model_name TEXT, model TEXT, slot INTEGER,
            bucket INTEGER, params TEXT, key_fp TEXT, updated_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT,
            actor TEXT, target TEXT, source TEXT, detail TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        _READY[path] = (SCHEMA_VERSION, time.time())


def _migrate(conn, cur):
    """逐版本推进；每个版本一个事务，失败即整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if cur < 1:
            pass                        # v1：初始 schema（建表在 _ensure_schema 里）
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 文档读写
def read_doc(name, path=None):
    """读文档（dict）；不存在返回 None。SQLite 不可用 → 抛异常由调用方回退。"""
    conn = _connect(path)
    try:
        row = conn.execute("SELECT doc FROM documents WHERE name=?", (name,)).fetchone()
        return json.loads(row["doc"]) if row else None
    finally:
        conn.close()


def write_doc(name, doc, mirror_path=None, mode=0o600):
    """事务写文档；可选镜像回 JSON 文件（原子写）。返回 True=已写。"""
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO documents(name, doc, updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET doc=excluded.doc, updated_at=excluded.updated_at",
                     (name, payload, _now_iso()))
        if name == "pool-config":
            _sync_accounts(conn, doc)
            _sync_deployments(conn, doc)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
    if mirror_path:
        _atomic_write(mirror_path, payload, mode)
    return True


def _sync_accounts(conn, doc):
    conn.execute("DELETE FROM accounts")
    for acc in (doc.get("accounts") or []):
        if not isinstance(acc, dict):
            continue
        try:
            slot = int(acc.get("slot"))
        except Exception:
            continue
        conn.execute("INSERT OR REPLACE INTO accounts(slot, key_var, email, enabled, key_fp, updated_at)"
                     " VALUES(?,?,?,?,?,?)",
                     (slot, str(acc.get("key_var") or ""), str(acc.get("email") or ""),
                      1 if acc.get("enabled") else 0, str(acc.get("key_fp") or ""), _now_iso()))


def _sync_deployments(conn, doc):
    conn.execute("DELETE FROM deployments")
    for dep in (doc.get("deployments") or []):
        if not isinstance(dep, dict):
            continue
        ident = str(dep.get("identity") or dep.get("model_name") or "")
        if not ident:
            continue
        conn.execute("INSERT OR REPLACE INTO deployments"
                     "(identity, model_name, model, slot, bucket, params, key_fp, updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (ident, str(dep.get("model_name") or ""), str(dep.get("model") or ""),
                      dep.get("slot"), dep.get("bucket"),
                      json.dumps(dep.get("litellm_params") or {}, ensure_ascii=False, sort_keys=True),
                      str(dep.get("key_fp") or ""), _now_iso()))


def write_deployments(entries, updated_at=None):
    """整体替换 deployments 表（生成器每次渲染后调用）。

    条目字段：identity / model_name / model / slot / bucket / params(dict) / key_fp。
    只存参数形状与变量名（api_key_env），**绝不存明文密钥**。
    """
    ts = updated_at or _now_iso()
    entries = list(entries or [])
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM deployments")
        for e in entries:
            ident = str(e.get("identity") or e.get("model_name") or "")
            if not ident:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO deployments"
                "(identity, model_name, model, slot, bucket, params, key_fp, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (ident, str(e.get("model_name") or ""), str(e.get("model") or ""),
                 e.get("slot"), e.get("bucket"),
                 json.dumps(e.get("params") or {}, ensure_ascii=False, sort_keys=True),
                 str(e.get("key_fp") or ""), ts))
        conn.execute("COMMIT")
        return len(entries)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 审计 / 事件
def append_audit(record):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO audit(ts, action, actor, target, source, detail) VALUES(?,?,?,?,?,?)",
                     (str(record.get("ts") or _now_iso()), str(record.get("action") or ""),
                      str(record.get("actor") or ""), str(record.get("target") or ""),
                      str(record.get("source") or ""),
                      json.dumps(record.get("detail"), ensure_ascii=False, default=str)))
        conn.execute("DELETE FROM audit WHERE id <= (SELECT MAX(id)-? FROM audit)", (AUDIT_MAX,))
        conn.execute("COMMIT")
    finally:
        conn.close()


def append_event(kind, payload=None):
    conn = _connect()
    try:
        conn.execute("INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
                     (_now_iso(), str(kind), json.dumps(payload or {}, ensure_ascii=False, default=str)))
    finally:
        conn.close()


def recent_audit(limit=20):
    conn = _connect()
    try:
        rows = conn.execute("SELECT ts, action, actor, target, source FROM audit"
                            " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def recent_events(limit=20):
    conn = _connect()
    try:
        rows = conn.execute("SELECT ts, kind, payload FROM events ORDER BY id DESC LIMIT ?",
                            (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"] or "{}")
            except Exception:
                pass
            out.append(d)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- 迁移
def import_json_doc(path, name, force=False):
    """把一个既有 JSON 文件导入 SQLite（幂等：已存在且非 force 则跳过）。"""
    if not os.path.isfile(path):
        return False
    if not force and read_doc(name) is not None:
        return False
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    write_doc(name, doc, mirror_path=None)
    return True


def migrate_from_files(home=None):
    """把 pool-config.json / .pool-reconciler-state.json 导入；审计历史按行导入。"""
    home = home or pool_home()
    stats = {"config": False, "state": False, "audit": 0}
    stats["config"] = import_json_doc(os.path.join(home, "pool-config.json"), "pool-config")
    stats["state"] = import_json_doc(os.path.join(home, ".pool-reconciler-state.json"),
                                     "reconciler-state")
    audit_path = os.path.join(home, "admin-audit.jsonl")
    if os.path.isfile(audit_path):
        conn = _connect()
        try:
            have = conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
            if have == 0:
                conn.execute("BEGIN IMMEDIATE")
                n = 0
                with open(audit_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        conn.execute("INSERT INTO audit(ts, action, actor, target, source, detail)"
                                     " VALUES(?,?,?,?,?,?)",
                                     (str(rec.get("ts") or ""), str(rec.get("action") or ""),
                                      str(rec.get("actor") or ""), str(rec.get("target") or ""),
                                      str(rec.get("source") or ""),
                                      json.dumps(rec.get("detail"), ensure_ascii=False, default=str)))
                        n += 1
                conn.execute("COMMIT")
                stats["audit"] = n
        finally:
            conn.close()
    return stats


# ---------------------------------------------------------------- 看板查询
def snapshot():
    """给看板用的只读快照：账号/部署/最近审计（不解析 config.yaml）。"""
    conn = _connect()
    try:
        acc = [dict(r) for r in conn.execute(
            "SELECT slot, key_var, email, enabled, key_fp, updated_at FROM accounts ORDER BY slot")]
        dep = conn.execute("SELECT COUNT(*) c FROM deployments").fetchone()["c"]
        by_model = [dict(r) for r in conn.execute(
            "SELECT model_name, COUNT(*) n FROM deployments GROUP BY model_name ORDER BY n DESC")]
        last_audit = [dict(r) for r in conn.execute(
            "SELECT ts, action, actor, target FROM audit ORDER BY id DESC LIMIT 10")]
        return {"accounts": acc, "deployments": dep, "by_model": by_model,
                "recent_audit": last_audit}
    finally:
        conn.close()


def health():
    """自检：库可写/表齐全/文档计数。"""
    conn = _connect()
    try:
        q = lambda sql: conn.execute(sql).fetchone()[0]
        return {"db": db_path(), "schema_version": q("SELECT value FROM meta WHERE key='schema_version'"),
                "documents": q("SELECT COUNT(*) FROM documents"),
                "accounts": q("SELECT COUNT(*) FROM accounts"),
                "deployments": q("SELECT COUNT(*) FROM deployments"),
                "audit": q("SELECT COUNT(*) FROM audit"),
                "events": q("SELECT COUNT(*) FROM events"),
                "journal": q("PRAGMA journal_mode")}
    finally:
        conn.close()


# ---------------------------------------------------------------- 小工具
def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _atomic_write(path, payload, mode):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "health"
    if cmd == "health":
        print(json.dumps(health(), ensure_ascii=False, indent=2))
    elif cmd == "migrate":
        print(json.dumps(migrate_from_files(), ensure_ascii=False, indent=2))
    elif cmd == "snapshot":
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2, default=str))
    elif cmd == "audit":
        print(json.dumps(recent_audit(int(sys.argv[2]) if len(sys.argv) > 2 else 10),
                         ensure_ascii=False, indent=2))
    else:
        raise SystemExit("用法: pool_store.py [health|migrate|snapshot|audit [n]]")
