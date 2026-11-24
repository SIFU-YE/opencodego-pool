#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sidecar 控制面门禁回归测试：allowlist 热门禁（404）/ /v1/models 过滤 / 账号禁用过滤。

覆盖（全部本地、不联网）：
  1) allowlist 内模型        → 原样透传，上游收到请求
  2) allowlist 外模型        → 404 model_not_found，且**未打到上游**（chat 与 responses 两种协议）
  3) 池内别名 <model>@a<N>[s<K>] → 不误拦，正常透传
  4) 控制文件变更后 TTL 内自动生效（新增模型放行、移除模型拒绝），**不重启 sidecar**
  5) 控制面不可用（load_config 返回 None / import 直接失败）→ fail-open 透传 + /v1/models 回退硬编码
  6) GET /v1/models 按 allowlist 过滤，且别名绝不外泄
  7) 账号 enabled:false → 选号命中该槽位时放弃别名改写，退回默认型号（日志 acct-disabled skip slot=N）

做法（参考 test_sidecar_stream_termination.py 的子进程/固定端口方案）：
  · 127.0.0.1:4000 起假上游（记录每次命中，可按需返回模型表）；
  · 临时目录里放一个 `pool_control.py` 桩（正常 / 返回 None / import 报错三种）与
    `pool-config.json`，并用 `POOL_HOME` / `POOL_CONFIG_PATH` / `POOL_CONFIG_TTL` 注入；
  · 用 launcher 以子进程启动**真实的** session-sidecar.py（sys.path 首位是桩目录，
    因此即使仓库里已有真实 pool_control.py 也不会被这个测试用到，测试是自洽的）；
  · 桩的 TTL 与冻结契约一致（默认 2.0s，POOL_CONFIG_TTL 可覆盖）。

用法：python3 pool/tools/test_sidecar_allowlist.py
      SIDECAR_PATH=<待测的 session-sidecar.py> 可指向旧版本验证会失败。
退出码 0 = 全过。
"""
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_DIR = os.path.abspath(os.path.join(HERE, ".."))
SIDECAR = os.path.abspath(os.environ.get("SIDECAR_PATH") or os.path.join(POOL_DIR, "session-sidecar.py"))
UP_PORT = 4000
SIDE_PORT = 4001

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)
    return bool(cond)


def port_busy(port):
    s = socket.socket()
    try:
        s.settimeout(0.4)
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port_free(port, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not port_busy(port):
            return True
        time.sleep(0.1)
    return not port_busy(port)


# ================================================================ pool_control 桩（三种形态）
STUB_NORMAL = r'''# -*- coding: utf-8 -*-
"""测试桩：复刻 pool/pool_control.py 的冻结契约（2s TTL、POOL_CONFIG_PATH 优先）。"""
import json
import os
import time

_TTL = float(os.environ.get("POOL_CONFIG_TTL") or 2.0)
_HOME = os.environ.get("POOL_HOME") or "/opt/litellm"
_STATE = {"path": None, "at": 0.0, "cfg": None}


def _path():
    return os.environ.get("POOL_CONFIG_PATH") or os.path.join(_HOME, "pool-config.json")


def load_config(force=False):
    path = _path()
    now = time.time()
    if (not force) and _STATE["path"] == path and (now - _STATE["at"]) < _TTL:
        return _STATE["cfg"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            doc = None
    except Exception:
        doc = None
    _STATE["path"] = path
    _STATE["at"] = now
    _STATE["cfg"] = doc
    return doc


def last_error():
    return "stub"
'''
STUB_UNAVAILABLE = (
    "# -*- coding: utf-8 -*-\n"
    "\"\"\"测试桩：控制面不可用（配置读不到 → load_config() 返回 None）。\"\"\"\n"
    "def load_config(force=False):\n    return None\n\n\n"
    "def last_error():\n    return \"stub-unavailable\"\n"
)
STUB_BROKEN = (
    "# -*- coding: utf-8 -*-\n"
    "\"\"\"测试桩：import 阶段直接失败，验证 sidecar 优雅降级。\"\"\"\n"
    "raise ImportError(\"pool_control import broken (test stub)\")\n"
)
LAUNCHER = r'''# -*- coding: utf-8 -*-
"""测试 launcher：桩目录保持在 sys.path 首位（优先于仓库真实 pool_control），再运行 sidecar。"""
import runpy
import sys

sys.path.append(sys.argv[2])                     # pool/（toolrepair 等同级依赖）
runpy.run_path(sys.argv[1], run_name="__main__")
'''


def make_stub_dir(kind="normal"):
    d = tempfile.mkdtemp(prefix="sc-gate-%s-" % kind)
    src = {"normal": STUB_NORMAL, "unavailable": STUB_UNAVAILABLE, "broken": STUB_BROKEN}[kind]
    with open(os.path.join(d, "pool_control.py"), "w", encoding="utf-8") as fh:
        fh.write(src)
    with open(os.path.join(d, "launch_sidecar.py"), "w", encoding="utf-8") as fh:
        fh.write(LAUNCHER)
    return d


def cfg_path(d):
    return os.path.join(d, "pool-config.json")


def write_cfg(d, allow):
    """原子写 pool-config.json（只关心 models.allow）。"""
    p = cfg_path(d)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"models": {"allow": list(allow)}, "accounts": []}, fh)
    os.replace(tmp, p)


def start_sidecar(d):
    env = os.environ.copy()
    env["POOL_HOME"] = d
    env["POOL_CONFIG_PATH"] = cfg_path(d)
    env["POOL_CONFIG_TTL"] = "2.0"
    logpath = os.path.join(d, "sidecar.log")
    logf = open(logpath, "wb")
    proc = subprocess.Popen([sys.executable, os.path.join(d, "launch_sidecar.py"), SIDECAR, POOL_DIR],
                            cwd=d, env=env, stdout=logf, stderr=subprocess.STDOUT)
    logf.close()
    for _ in range(100):
        if proc.poll() is not None:
            break
        if port_busy(SIDE_PORT):
            return proc, logpath
        time.sleep(0.1)
    return proc, logpath


def log_tail(path, n=25):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except Exception:
        return "(no log)"


def stop_sidecar(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    wait_port_free(SIDE_PORT, 8.0)


# ================================================================ 假上游（127.0.0.1:4000）
class FakeUpstream:
    def __init__(self, port=UP_PORT):
        self.hits = []                      # ("POST"|"GET", path[, body])
        self.lock = threading.Lock()
        self.models = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, payload, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()

            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n > 0 else b""

            def do_GET(self):
                with outer.lock:
                    outer.hits.append(("GET", self.path))
                if "/v1/models" in self.path:
                    doc = {"object": "list", "data": [dict(m) for m in outer.models]}
                    self._send(200, json.dumps(doc).encode("utf-8"))
                else:
                    self._send(404, b'{"error":{"message":"not found"}}')

            def do_POST(self):
                body = self._read()
                with outer.lock:
                    outer.hits.append(("POST", self.path, body))
                model = None
                try:
                    model = (json.loads(body) or {}).get("model")
                except Exception:
                    pass
                self._send(200, json.dumps({"ok": True, "path": self.path, "model": model}).encode())

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.hits = []

    def count(self, method, path=None):
        with self.lock:
            return sum(1 for h in self.hits if h[0] == method and (path is None or h[1] == path))

    def set_models(self, ids):
        self.models = [{"id": i, "object": "model"} for i in ids]

    def shutdown(self):
        self.srv.shutdown()


def call(method, path, body=None, timeout=10.0):
    """经 sidecar:4001 发请求；body 传 dict 时自动 JSON 编码。"""
    payload = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
    c = http.client.HTTPConnection("127.0.0.1", SIDE_PORT, timeout=timeout)
    try:
        c.request(method, path, body=payload, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, (r.getheader("Content-Type") or ""), r.read()
    finally:
        c.close()


def chat(model, content="ping"):
    return call("POST", "/v1/chat/completions", {"model": model,
                                                 "messages": [{"role": "user", "content": content}]})


DENY_WANT = {"error": {"message": "model not found",
                       "type": "invalid_request_error", "code": "model_not_found"}}


def is_deny(status, ctype, data):
    if status != 404 or "json" not in (ctype or "").lower():
        return False
    try:
        return json.loads(data.decode("utf-8")) == DENY_WANT
    except Exception:
        return False


def poll_status(model, want, t_write, timeout=6.0):
    """轮询直到 chat(model) 的状态 == want；返回 (是否达成, 距写盘秒数, 最后状态)。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = chat(model, "ttl-probe")[0]
        if last == want:
            return True, time.time() - t_write, last
        time.sleep(0.1)
    return False, time.time() - t_write, last


# ================================================================ 端到端场景
def run_e2e(up):
    # ---------- 场景组 A：控制面正常（allow = mimo-v2.6-flash, deepseek-v4.1-flash） ----------
    dA = make_stub_dir("normal")
    write_cfg(dA, ["mimo-v2.6-flash", "deepseek-v4.1-flash"])
    proc, logs = start_sidecar(dA)
    try:
        if not ok("A. sidecar 已监听 %d（控制面正常）" % SIDE_PORT, port_busy(SIDE_PORT), log_tail(logs)):
            return
        up.reset()
        up.set_models(["mimo-v2.6-flash", "mimo-v2.6-pro", "deepseek-v4.1-flash",
                       "muse-spark-1.3-contributor", "claude-haiku-5-5", "not-in-pool-9000",
                       "mimo-v2.6-flash@a1s2"])

        # 1) allowlist 内 → 透传
        st, _, data = chat("mimo-v2.6-flash")
        hit_chat = up.count("POST", "/v1/chat/completions")
        ok("1) allowlist 内模型透传（200 且上游收到）", st == 200 and hit_chat == 1,
           "status=%s upstream_chat=%d" % (st, hit_chat))

        # 2) allowlist 外 → 404 且未打到上游（chat 与 responses）
        st2, ct2, d2 = chat("not-in-pool-9000")
        ok("2a) allowlist 外模型 → 404 + OpenAI 错误体", is_deny(st2, ct2, d2),
           "status=%s body=%s" % (st2, d2[:120]))
        ok("2b) 被拒请求未打到上游", up.count("POST", "/v1/chat/completions") == hit_chat,
           "chat=%d" % up.count("POST", "/v1/chat/completions"))
        st2r, ct2r, d2r = call("POST", "/v1/responses",
                               {"model": "claude-haiku-5-5", "input": "hi"})
        ok("2c) /v1/responses 同样 404 且未打到上游",
           is_deny(st2r, ct2r, d2r) and up.count("POST", "/v1/responses") == 0,
           "status=%s upstream_responses=%d body=%s" % (st2r, up.count("POST", "/v1/responses"),
                                                        d2r[:100]))

        # 3) 池内别名不误拦（桶别名 + 旧式别名）
        st3, _, _ = chat("mimo-v2.6-flash@a2s3")
        st3b, _, _ = chat("deepseek-v4.1-flash@a3")
        ok("3) 池内别名（@a2s3 / @a3）不被误拦", st3 == 200 and st3b == 200,
           "s3=%s legacy=%s" % (st3, st3b))

        # 6) /v1/models 按 allowlist 过滤 + 别名不外泄
        st6, _, d6 = call("GET", "/v1/models")
        ids = []
        try:
            ids = [m.get("id") for m in json.loads(d6.decode("utf-8")).get("data", [])]
        except Exception:
            pass
        ok("6) /v1/models 按 allowlist 过滤、别名不外泄",
           st6 == 200 and sorted(ids) == ["deepseek-v4.1-flash", "mimo-v2.6-flash"], "ids=%s" % ids)

        # 4) TTL 热生效：新增放行 + 移除拒绝，均不重启
        time.sleep(2.2)                                   # 让桩缓存过期
        chat("mimo-v2.6-flash", "prime-add")              # 预热缓存（此刻读到旧 allowlist）
        t_write = time.time()
        write_cfg(dA, ["mimo-v2.6-flash", "deepseek-v4.1-flash", "brand-new-model-xyz"])
        st_imm, _, d_imm = chat("brand-new-model-xyz", "immediate")
        ok("4a) 写盘瞬间仍在 TTL 内（缓存旧值 → 404）", is_deny(st_imm, "application/json", d_imm),
           "status=%s" % st_imm)
        got_add, delay_add, last_add = poll_status("brand-new-model-xyz", 200, t_write, timeout=6.0)
        ok("4b) 新增模型 ≤TTL+ε 内自动放行（热生效，无需重启）", got_add,
           "生效延迟=%.2fs last=%s" % (delay_add, last_add))

        time.sleep(2.2)                                   # 再让缓存过期
        chat("deepseek-v4.1-flash", "prime-remove")       # 预热缓存（此刻仍允许 deepseek）
        t_write2 = time.time()
        write_cfg(dA, ["mimo-v2.6-flash", "brand-new-model-xyz"])
        st_imm2, _, _ = chat("deepseek-v4.1-flash", "immediate")
        ok("4c) 写盘瞬间移除尚未生效（缓存旧值 → 200）", st_imm2 == 200, "status=%s" % st_imm2)
        got_rm, delay_rm, last_rm = poll_status("deepseek-v4.1-flash", 404, t_write2, timeout=6.0)
        ok("4d) 移除模型 ≤TTL+ε 内自动拒绝（热生效，无需重启）", got_rm,
           "生效延迟=%.2fs last=%s" % (delay_rm, last_rm))
        ok("4e) 热生效延迟落在 TTL 语义区间（1.0s ≤ 延迟 ≤ TTL+1.0s）",
           1.0 <= delay_add <= 3.0 and 1.0 <= delay_rm <= 3.0,
           "add=%.2fs remove=%.2fs (stub TTL=2.0s)" % (delay_add, delay_rm))
        print("        · 实测热生效：新增 %.2fs / 移除 %.2fs（桩 TTL=2.0s，含轮询粒度 0.1s）"
              % (delay_add, delay_rm))
    finally:
        stop_sidecar(proc)
        shutil.rmtree(dA, ignore_errors=True)

    # ---------- 场景组 B：控制面不可用（load_config → None）→ fail-open ----------
    dB = make_stub_dir("unavailable")
    procB, logsB = start_sidecar(dB)
    try:
        if ok("B. sidecar 已监听 %d（控制面不可用）" % SIDE_PORT, port_busy(SIDE_PORT), log_tail(logsB)):
            up.reset()
            up.set_models(["mimo-v2.6-flash", "deepseek-v4.1-flash", "not-in-pool-9000",
                           "mimo-v2.6-flash@a1s2"])
            stB, _, dB_body = chat("whatever-not-configured-9000")
            ok("5a) 控制面不可用 → fail-open 透传（不得 404）", stB == 200,
               "status=%s body=%s" % (stB, dB_body[:100]))
            stB2, _, dB2 = call("GET", "/v1/models")
            idsB = []
            try:
                idsB = [m.get("id") for m in json.loads(dB2.decode("utf-8")).get("data", [])]
            except Exception:
                pass
            ok("5b) 控制面不可用 → /v1/models 回退硬编码集合（别名不外泄）",
               stB2 == 200 and sorted(idsB) == ["deepseek-v4.1-flash", "mimo-v2.6-flash"],
               "ids=%s" % idsB)
    finally:
        stop_sidecar(procB)
        shutil.rmtree(dB, ignore_errors=True)

    # ---------- 场景组 C：pool_control import 直接失败 → 优雅降级、不启动失败 ----------
    dC = make_stub_dir("broken")
    procC, logsC = start_sidecar(dC)
    try:
        alive = port_busy(SIDE_PORT)
        ok("C. pool_control import 失败也不影响启动（fail-open）", alive, log_tail(logsC))
        if alive:
            up.reset()
            stC, _, dC_body = chat("anything-9001")
            ok("5c) import 失败 → 原样透传", stC == 200, "status=%s body=%s" % (stC, dC_body[:100]))
    finally:
        stop_sidecar(procC)
        shutil.rmtree(dC, ignore_errors=True)


# ================================================================ 进程内单测：映射协议 + 账号禁用
def load_sidecar_module():
    if POOL_DIR not in sys.path:
        sys.path.insert(0, POOL_DIR)
    spec = importlib.util.spec_from_file_location("sc_allowlist_gate", SIDECAR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_units():
    if not os.path.exists(SIDECAR):
        return
    sc = load_sidecar_module()

    # --- 冻结契约兼容：MappingProxyType 快照（pool_control 的真实返回类型）---
    frozen = types.MappingProxyType({
        "models": types.MappingProxyType({"allow": ("mimo-v2.6-flash",)}),
        "accounts": (types.MappingProxyType({"slot": 2, "key_var": "OCG_KEY_2", "enabled": False}),
                     types.MappingProxyType({"slot": 3, "key_var": "OCG_KEY_3", "enabled": True})),
    })
    real_ctrl = sc.control_config
    sc.control_config = lambda: frozen
    ok("U1) allow_models 支持 MappingProxyType+tuple 快照",
       sc.allow_models() == {"mimo-v2.6-flash"}, str(sc.allow_models()))
    ok("U2) disabled_slots 只收 enabled:false 的槽位（支持快照）",
       sc.disabled_slots() == {2}, str(sc.disabled_slots()))
    ok("U3) deny_model 命中 allowlist 外模型返回固定错误体",
       sc.deny_model(json.dumps({"model": "other"}).encode(), "POST", "/v1/chat/completions")
       == sc.DENY_MODEL_BODY)
    ok("U4) deny_model 放行 allowlist 内模型",
       sc.deny_model(json.dumps({"model": "mimo-v2.6-flash"}).encode(), "POST",
                     "/v1/chat/completions") is None)
    ok("U5) deny_model 放行池内别名",
       sc.deny_model(json.dumps({"model": "other@a2s3"}).encode(), "POST",
                     "/v1/chat/completions") is None)
    ok("U6) deny_model 不拦非推理请求（GET /v1/models）",
       sc.deny_model(b'{"model":"other"}', "GET", "/v1/models") is None)
    ok("U7) deny_model 坏 JSON / 缺 model → 放行",
       sc.deny_model(b"{not json", "POST", "/v1/chat/completions") is None
       and sc.deny_model(b'{"messages":[]}', "POST", "/v1/chat/completions") is None)
    sc.control_config = lambda: None
    ok("U8) 控制面不可用 → allow_models/disabled_slots 均为 None（fail-open）",
       sc.allow_models() is None and sc.disabled_slots() is None
       and sc.deny_model(json.dumps({"model": "any"}).encode(), "POST",
                         "/v1/chat/completions") is None)
    sc.control_config = lambda: {"models": {"allow": ()}, "accounts": ()}
    ok("U9) allowlist 为空 → fail-open、不过滤账号",
       sc.allow_models() is None and sc.disabled_slots() == set()
       and sc.deny_model(json.dumps({"model": "any"}).encode(), "POST",
                         "/v1/chat/completions") is None)
    sc.control_config = real_ctrl

    # --- 账号禁用过滤：命中禁用槽位则不写 key_want、不改写成该槽位别名 ---
    tmp = tempfile.mkdtemp(prefix="sc-gate-unit-")
    alias_cfg = os.path.join(tmp, "config.yaml")
    tree = "gate-unit-1"
    bucketed = "mimo-v2.6-flash@a2s%d" % sc.session_bucket(tree)   # 桶号由树键哈希决定，动态构造
    with open(alias_cfg, "w", encoding="utf-8") as fh:
        fh.write("model_list:\n  - model_name: %s\n  - model_name: mimo-v2.6-flash@a2\n" % bucketed)
    old_cfgfile = sc.AFFINITY_CONFIG_FILE
    old_aff = sc.affinity_state
    old_slog = sc.slog
    logs = []
    sc.AFFINITY_CONFIG_FILE = alias_cfg
    sc.affinity_state = lambda: {"mode": "full", "accounts": [("OCG_KEY_2", 1.0)], "reason": "unit"}
    sc.slog = lambda m: logs.append(str(m))
    body = json.dumps({"model": "mimo-v2.6-flash",
                       "messages": [{"role": "user", "content": "hi [pool-tree: %s]" % tree}]}).encode()
    try:
        sc._ALIAS_CACHE.update({"checked": 0.0, "set": set(), "ok": False, "reason": "unit"})
        sc._ACCT_SKIP_LAST.clear()
        sc.control_config = lambda: {"models": {"allow": ["mimo-v2.6-flash"]},
                                     "accounts": [{"slot": 2, "enabled": False}]}
        out, sess = sc.process_request(body)
        parsed = json.loads(out.decode("utf-8")) if out else {}
        md = parsed.get("metadata", {})
        ok("U10) 禁用槽位：不改写成别名、不写 key_want",
           parsed.get("model") == "mimo-v2.6-flash"
           and md.get("key_want") is None and md.get("pool_alias") is None,
           "model=%s md=%s" % (parsed.get("model"), md))
        ok("U11) 禁用槽位日志含 `acct-disabled skip slot=2`",
           any("acct-disabled skip slot=2" in x for x in logs), str(logs[-3:]))

        sc._ALIAS_CACHE.update({"checked": 0.0, "set": set(), "ok": False, "reason": "unit"})
        sc.control_config = lambda: {"models": {"allow": ["mimo-v2.6-flash"]},
                                     "accounts": [{"slot": 2, "enabled": True}]}
        out2, _ = sc.process_request(body)
        parsed2 = json.loads(out2.decode("utf-8")) if out2 else {}
        md2 = parsed2.get("metadata", {})
        ok("U12) 槽位启用：恢复别名改写 + key_want",
           parsed2.get("model") == bucketed
           and md2.get("key_want") == "OCG_KEY_2" and md2.get("pool_alias") == bucketed,
           "model=%s md=%s" % (parsed2.get("model"), md2))

        sc._ALIAS_CACHE.update({"checked": 0.0, "set": set(), "ok": False, "reason": "unit"})
        sc.control_config = lambda: None                     # 控制面不可用：不做过滤
        out3, _ = sc.process_request(body)
        parsed3 = json.loads(out3.decode("utf-8")) if out3 else {}
        ok("U13) 控制面不可用：不做账号过滤（仍按原逻辑改写别名）",
           parsed3.get("model") == bucketed, "model=%s" % parsed3.get("model"))
    except Exception as e:  # noqa: BLE001
        ok("U10-U13) 账号禁用过滤单测执行", False, "%s: %s" % (type(e).__name__, e))
    finally:
        sc.AFFINITY_CONFIG_FILE = old_cfgfile
        sc.affinity_state = old_aff
        sc.slog = old_slog
        sc.control_config = real_ctrl
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if not os.path.exists(SIDECAR):
        print("SKIP: sidecar not found at %s" % SIDECAR)
        return 0
    for p in (UP_PORT, SIDE_PORT):
        if port_busy(p):
            print("SKIP: 端口 %d 已被占用（本机可能有其它实例）" % p)
            return 0

    print("— 进程内单测：控制面结构兼容 / allowlist 判定 / 账号禁用过滤 —")
    run_units()

    print("\n— 端到端：假上游 + 真实 sidecar 子进程 + 临时 pool-config.json —")
    up = FakeUpstream(UP_PORT)
    try:
        run_e2e(up)
    finally:
        up.shutdown()

    print()
    if fails:
        print("FAILED %d: %s" % (len(fails), fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
