#!/usr/bin/env python3
"""回归：接口 single-flight+TTL 缓存 / 请求日志宿主机直读增量 / 会话文件解析缓存

对应 2026-10-17 修复：
  · /api/summary 与 /api/account-sessions 由「每请求全量重算」改为 single-flight + TTL；
  · litellm_request_log() 由 docker exec 全量 cat 改为宿主机 bind-mount 直读 + 增量；
  · scan_dsh_sessions() 按 (mtime,size) 缓存单文件解析，未改动的会话不再解压。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard as D  # noqa: E402


class CachedJsonTest(unittest.TestCase):
    def setUp(self):
        D._JSON_CACHE.clear()
        D._JSON_LOCKS.clear()

    def test_ttl_hits_once(self):
        n = {"c": 0}

        def fn():
            n["c"] += 1
            return {"v": n["c"]}

        a = D._cached_json("t", 60, fn)
        b = D._cached_json("t", 60, fn)
        self.assertEqual(n["c"], 1, "TTL 内应只计算一次")
        self.assertEqual(a, b)
        self.assertEqual(json.loads(a), {"v": 1})

    def test_ttl_expiry_recomputes(self):
        n = {"c": 0}

        def fn():
            n["c"] += 1
            return {"v": n["c"]}

        D._cached_json("t2", 0.2, fn)
        time.sleep(0.25)
        D._cached_json("t2", 0.2, fn)
        self.assertEqual(n["c"], 2)

    def test_single_flight_under_concurrency(self):
        n = {"c": 0}
        lock = threading.Lock()

        def fn():
            with lock:
                n["c"] += 1
            time.sleep(0.12)
            return {"v": 1}

        outs = []
        ths = [threading.Thread(target=lambda: outs.append(D._cached_json("t3", 60, fn)))
               for _ in range(8)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(n["c"], 1, "8 个并发请求应只算一次（single-flight）")
        self.assertEqual(len(set(outs)), 1)


class ReqLogIncrementalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "pool-requests.jsonl")
        self._old = D.LITELLM_REQLOG_HOST
        D.LITELLM_REQLOG_HOST = self.path
        D._REQLOG_ST.update({"key": None, "off": 0, "rows": None})

    def tearDown(self):
        D.LITELLM_REQLOG_HOST = self._old
        D._REQLOG_ST.update({"key": None, "off": 0, "rows": None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rows, mode="a"):
        with open(self.path, mode, encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_first_read_and_reuse(self):
        self._write([{"model": "a", "tt": 1}, {"model": "b", "tt": 2}], "w")
        self.assertEqual([r["model"] for r in D.litellm_request_log()], ["a", "b"])
        self.assertEqual(len(D.litellm_request_log()), 2)

    def test_only_new_lines_are_parsed(self):
        self._write([{"model": "a"}], "w")
        D.litellm_request_log()
        self._write([{"model": "b"}, {"model": "c"}])
        rows = D.litellm_request_log()
        self.assertEqual([r["model"] for r in rows], ["a", "b", "c"])
        # 没增长 → 直接复用，不重复解析
        off = D._REQLOG_ST["off"]
        D.litellm_request_log()
        self.assertEqual(D._REQLOG_ST["off"], off)

    def test_partial_trailing_line_waits(self):
        self._write([{"model": "a"}], "w")
        D.litellm_request_log()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"model": "b"}')          # 半行（还没换行）
        self.assertEqual(len(D.litellm_request_log()), 1, "半行不应被解析")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n")
        self.assertEqual([r["model"] for r in D.litellm_request_log()], ["a", "b"])

    def test_truncate_reloads(self):
        self._write([{"model": "a"}, {"model": "b"}], "w")
        D.litellm_request_log()
        self._write([{"model": "z"}], "w")     # 截断重写（小于上次偏移）
        self.assertEqual([r["model"] for r in D.litellm_request_log()], ["z"])

    def test_missing_host_file_falls_back(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.assertIsNone(D._reqlog_read_host(), "宿主机不可用时应返回 None 交给 docker 兜底")
        # 兜底路径本身：docker 也失败 → 空列表（绝不抛异常）
        old_run = D.subprocess.run

        def boom(*a, **k):
            raise OSError("docker unavailable in test")
        D.subprocess.run = boom
        try:
            self.assertEqual(D.litellm_request_log(), [])
        finally:
            D.subprocess.run = old_run


class SessionScanCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        d = os.path.join(self.tmp, "ws1", "session-abc")
        os.makedirs(d)
        self.f = os.path.join(d, "session.v4.jsonl.zstd")
        with open(self.f, "wb") as fh:
            fh.write(b"not-really-zstd")
        self._old_sessions, self._old_run = D.DSH_SESSIONS, D.subprocess.run
        D.DSH_SESSIONS = self.tmp
        D._SESS_FILE_CACHE.clear()
        self.calls = {"n": 0}

    def tearDown(self):
        D.DSH_SESSIONS = self._old_sessions
        D.subprocess.run = self._old_run
        D._SESS_FILE_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_run(self, *a, **k):
        self.calls["n"] += 1
        raise OSError("zstd unavailable in test")

    def test_unchanged_file_parsed_once(self):
        D.subprocess.run = self._fake_run
        self.assertEqual(D.scan_dsh_sessions(), [])
        self.assertEqual(D.scan_dsh_sessions(), [])
        self.assertEqual(self.calls["n"], 1, "未改动的会话文件不应再次解压")

    def test_modified_file_reparsed(self):
        D.subprocess.run = self._fake_run
        D.scan_dsh_sessions()
        os.utime(self.f, (time.time() + 5, time.time() + 5))   # mtime 变化
        D.scan_dsh_sessions()
        self.assertEqual(self.calls["n"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class AlignEquivalenceTest(unittest.TestCase):
    """±8s 对齐：二分索引版必须与旧的三重循环逐请求等价"""

    def _old_align(self, sessions, req):
        out = set()
        ts = req["ts"]
        for s in sessions:
            if s["model"] and req.get("model") and req["model"] != s["model"]:
                continue
            for at in s["assistantTs"]:
                if abs(at - ts) <= D.ALIGN_MS:
                    out.add(s["id"])
                    break
        return out

    def test_equivalence_with_reference(self):
        import random
        random.seed(7)
        sessions = []
        for i in range(40):
            sessions.append({
                "id": "s%d" % i,
                "model": random.choice(["m1", "m2", "", "m3"]),
                "assistantTs": [random.randint(0, 100000) for _ in range(random.randint(0, 6))],
            })
        idx = D._build_session_idx(sessions)
        for _ in range(400):
            req = {"ts": random.randint(0, 100000),
                   "model": random.choice(["m1", "m2", "", "m4", None])}
            self.assertEqual(
                D._align_sids(idx, req["model"] or "", req["ts"]),
                self._old_align(sessions, req),
                "对齐结果与旧实现不一致: %r" % (req,))

    def test_boundary(self):
        sessions = [{"id": "a", "model": "m", "assistantTs": [1000]},
                    {"id": "b", "model": "", "assistantTs": [900000]},
                    {"id": "c", "model": "m", "assistantTs": [900000]}]
        idx = D._build_session_idx(sessions)
        self.assertEqual(D._align_sids(idx, "m", 1000 + D.ALIGN_MS), {"a"})   # 闭区间边界
        self.assertEqual(D._align_sids(idx, "m", 1000 + D.ALIGN_MS + 1), set())
        self.assertEqual(D._align_sids(idx, "other", 900000), {"b"})   # 无模型会话不被模型过滤
        self.assertEqual(D._align_sids(idx, "", 900000), {"b", "c"})   # 请求无模型 → 全量
