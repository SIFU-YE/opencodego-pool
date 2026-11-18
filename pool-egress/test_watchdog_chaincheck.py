#!/usr/bin/env python3
"""watchdog 全链路体检（chain-check）回归测试 —— 2026-10-16 事故修复的配套测试。

被测行为：稳态 TCP 探测通过、但 current 节点全链路已死时，看门狗必须换节点并罚掉它。
不打补丁前的行为：只要 TCP 通就永远不换（本次 500 事故的直接原因）。

跑法：python3 /opt/pool-egress/test_watchdog_chaincheck.py
"""
import importlib.util
import os
import sys
import types

# 可移植定位：默认用本目录的 watchdog.py（仓库内）；服务器上可用 WATCHDOG_PATH 覆盖
HERE = os.path.dirname(os.path.abspath(__file__))
WD = os.path.abspath(os.environ.get("WATCHDOG_PATH") or os.path.join(HERE, "watchdog.py"))
if not os.path.exists(WD) and os.path.exists("/opt/pool-egress/watchdog.py"):
    WD = "/opt/pool-egress/watchdog.py"


def load_watchdog(path=WD):
    spec = importlib.util.spec_from_file_location("wd_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClock:
    """每次 monotonic() 前进 1s，sleep() 不阻塞 —— 让 5s 轮询在毫秒内跑完。"""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        self.t += 1.0
        return self.t

    def time(self):
        return self.t

    def sleep(self, _s):
        self.t += 5.0


def run_case(chain_healthy, max_calls=60):
    """驱动 main() 若干轮，返回 (日志行, api_switch 的调用参数)。"""
    wd = load_watchdog()
    logs, switches = [], []
    clock = FakeClock()
    calls = {"n": 0}

    wd.time = clock
    wd.log = logs.append
    wd.prune_quarantine = lambda quar: quar

    def api_get_now():
        calls["n"] += 1
        if calls["n"] > max_calls:
            raise SystemExit
        return "jp-aws01"

    def tcp_probe(_h, _p):
        return 59.0                      # 节点端口一直是通的（TCP 探测看不见链路故障）

    def delay_probe(_tag):
        return 120.0 if chain_healthy else None

    def run_sweep(tags, quar, fails, passes, ever_ok):
        ever_ok.update(tags)             # 让候选集合非空
        return {}

    def full_scan(nodes):
        return {t: 58.0 for t, _, _ in nodes}

    def api_switch(tag):
        switches.append(tag)
        return tag

    wd.api_get_now = api_get_now
    wd.tcp_probe = tcp_probe
    wd.delay_probe = delay_probe
    wd.run_sweep = run_sweep
    wd.full_scan = full_scan
    wd.api_switch = api_switch
    wd.load_quarantine = lambda: ({}, {})

    try:
        wd.main()
    except SystemExit:
        pass
    return logs, switches


def main():
    ok = True

    # 1) 链路死：必须换节点，且判据是 chain-fail（不是 fail/slow）
    logs, switches = run_case(chain_healthy=False)
    trigger = [l for l in logs if "trigger-chain-fail" in l]
    if not trigger:
        print("FAIL: 链路已死但看门狗没有动作；日志=" + repr(logs[:6]))
        ok = False
    elif not switches:
        print("FAIL: 有 chain-fail 判据但没有真的切节点")
        ok = False
    elif switches[0] == "jp-aws01":
        print("FAIL: 切到了故障节点自己")
        ok = False
    else:
        print("PASS: chain-fail 触发换节点 -> %s" % switches[0])
        print("      触发日志: %s" % trigger[0][:120])

    # 2) 罚掉故障节点：切走后不该再被选回来（CHAIN_PENALTY 窗口内）
    if switches.count("jp-aws01") > 0:
        print("FAIL: 故障节点在惩罚窗口内又被选回")
        ok = False
    else:
        print("PASS: 故障节点在惩罚窗口内未被重选（共换 %d 次）" % len(switches))

    # 3) 链路健康：不许乱切（防止修复引入抖动）
    logs2, switches2 = run_case(chain_healthy=True)
    if switches2:
        print("FAIL: 链路健康却切了节点 %r" % switches2)
        ok = False
    else:
        print("PASS: 链路健康时零切换（仅 TCP 稳态探测）")

    print("RESULT:", "ALL PASS" if ok else "FAILED")
    return 0 if ok else 1


def _preflight():
    """本测试驱动真实 watchdog 逻辑，需要一份有效的 pool-egress 配置。
    仓库内（无 /opt/pool-egress）应优雅跳过，而不是报 FileNotFoundError。"""
    cfg = os.environ.get("POOL_EGRESS_CFG", "/opt/pool-egress/config.json")
    if not os.path.exists(cfg):
        print("SKIP: 需要 pool-egress 的 config.json（当前 %s）；本测试设计在号池出口机上运行" % cfg)
        return False
    return True


if __name__ == "__main__":
    sys.exit(main() if _preflight() else 0)
