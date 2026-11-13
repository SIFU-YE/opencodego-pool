#!/usr/bin/env python3
"""dashboard.py 页面装配单测（不联网、不起服务、不碰生产路径）。

守住 2026-10-17 驾驶舱改版最容易回归的三件事：

1. `render_page()` **必须**把 `__ADMIN_PANEL__` 占位符替换掉 —— 否则页面上会直接
   漏出占位字符串；管理面模块缺失/版本不匹配时要降级成可读的占位说明；
2. 管理控制台片段里带足挂载点（`#adminBoot` / `window.adminInit`），
   保证「页签化」这条链路（服务端注入 → 前端懒初始化）没有断；
3. 看板骨架本身成立：6 个页签、双主题属性、管理面独立路由 `/admin` 已被移除。

运行： python3 test_dashboard_page.py -v
"""
import json
import os
import re
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import dashboard            # noqa: E402
import pool_admin_ui        # noqa: E402


class DashboardPageTest(unittest.TestCase):
    def test_01_placeholder_always_replaced(self):
        page = dashboard.render_page()
        self.assertNotIn("__ADMIN_PANEL__", page)
        # 页签骨架 + 关键 id 必须都在
        for marker in ('id="tab-admin"', 'id="adminBoot"', "window.adminInit",
                       'id="tab-overview"', 'id="alertBar"', 'id="acctDetail"',
                       'id="themeBtn"', 'id="refresh"'):
            self.assertIn(marker, page, marker)

    def test_02_tabs_and_theme(self):
        page = dashboard.render_page()
        tabs = re.findall(r'class="tab[^"]*" data-tab="([a-z]+)"', page)
        self.assertEqual(tabs, ["overview", "pool", "usage", "perf",
                                "routing", "admin"])
        # 旧书签 #config 必须被改写为 admin（否则会回退到总览）
        self.assertIn('if(want==="config")want="admin";', page)
        # 2026-10-20：DSH 配置页签已并入管理控制台，不得再出现独立页签
        self.assertNotIn('data-tab="config"', page)
        self.assertNotIn('id="tab-config"', page)
        admin_pane = page[page.index('id="tab-admin"'):]
        # 2026-10-31 弃用：DSH 配置同步 / Agent Teams 死配置已下线，面板与按钮不得再出现
        for gone in ('id="provsel"', 'id="teamsBox"', 'id="tcMM"', 'id="syncBtn"',
                     '<h2 class="sec">DSH 配置', '<h3>Agent Teams · 团队模型规则',
                     '<h3>写入 DSH 配置'):
            self.assertNotIn(gone, admin_pane, gone[:60])
        # 深浅双主题都定义了，且默认深色；主题标签必须靠 CSS 二选一显示
        self.assertIn('data-theme="dark"', page)
        self.assertIn('html[data-theme="light"]{', page)
        self.assertIn('html[data-theme="dark"] .t-light{display:none}', page)
        self.assertIn('html[data-theme="light"] .t-dark{display:none}', page)
        # 1920/2560 自适应靠 clamp，不应出现整页 transform 缩放
        self.assertIn("--gap:clamp(7px,.45vw,12px)", page)   # 2026-10-20 密度收紧
        # 2026-10-20 看板瘦身：折叠控件与 4 个稀疏/重复面板必须不存在
        self.assertNotIn('class="fold"', page)
        self.assertNotIn("foldAll", page)
        for gone in ("data-key=\"status\"", "data-key=\"acctsess\"",
                     "data-key=\"cache\"", "data-key=\"conc\""):
            self.assertNotIn(gone, page, gone)
        # 账号详情与配额面板之间必须有间距（用户反馈「第三排指标卡贴着配额框」）
        self.assertIn("#gridPool{margin-top:var(--gap)}", page)
        self.assertNotIn("transform:scale(", page)

    def test_03_admin_panel_content(self):
        page = dashboard.render_page()
        self.assertIn("管理控制台", page)
        for marker in ('id="modelsBox"', 'id="acctTbl"', 'id="keyTbl"',
                       'id="confirmPanel"', 'id="jobBox"', 'id="capChips"'):
            self.assertIn(marker, page, marker)
        # 密钥类输入必须是 password，且 bootstrap JSON 不能出现裸 </script>
        self.assertIn('type="password"', page)
        boot = re.search(r'<script id="adminBoot" type="application/json">(.*?)</script>',
                         page, re.S)
        self.assertIsNotNone(boot)
        self.assertNotIn("<", boot.group(1))

    def test_04_fallback_when_admin_ui_missing(self):
        saved = dashboard._pool_admin_ui
        dashboard._pool_admin_ui = None
        try:
            page = dashboard.render_page()
        finally:
            dashboard._pool_admin_ui = saved
        self.assertNotIn("__ADMIN_PANEL__", page)
        self.assertIn("管理控制台不可用", page)
        # 主看板不受影响
        self.assertIn('id="alertBar"', page)
        self.assertIn('id="themeBtn"', page)

    def test_05_admin_route_removed(self):
        """dashboard 侧不再把 /admin 当页面路由（pool_admin.dispatch 也不接管）。"""
        with open(os.path.join(HERE, "dashboard.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('elif self.path in ("/admin", "/admin/"):\n            self._send(200', src)
        self.assertIn("管理控制台已并入号池看板页签", src)
        try:
            import pool_admin
        except Exception as exc:                      # pragma: no cover
            self.skipTest("pool_admin 不可用: %s" % exc)
        app = pool_admin.AdminApp(control=None, home=HERE, reconciler="")
        self.assertFalse(app.dispatch(_FakeHandler("/admin"), "GET"))


class _FakeHandler:
    """只够 dispatch() 用来做路径判定的最小替身。"""

    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.wfile = None


class AccountThreeStateTest(unittest.TestCase):
    """2026-10-19 dashfix 回归：账号三态（在池 / 已禁用 / 暂存待启用）。

    报障「新增账号没有显示在看板中」的根因是 `ocg_keys()` 只认 `OCG_KEY_` 前缀，
    4 个 `OCG_PENDING_KEY_7..10` 全被漏掉。这里用临时 .env / pool-config.json /
    pool.db 复现三态，锁死「暂存账号必须可见且不进在池统计口径」。
    """

    def setUp(self):
        import sqlite3
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="pooltest-")
        self.env = os.path.join(self.tmp, ".env")
        self.pcfg = os.path.join(self.tmp, "pool-config.json")
        self.pdb = os.path.join(self.tmp, "pool.db")
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("OCG_KEY_1=REPLACE_ME\n")               # 占位值 → 视为未配置
            fh.write("OCG_KEY_2=k" + "x" * 49 + "\n")        # 已禁用槽位
            fh.write("OCG_KEY_4=k" + "y" * 49 + "\n")        # 在池（有 deployment）
            fh.write("OCG_EMAIL_2=a2@example.com\n")
            fh.write("OCG_EMAIL_4=a4@example.com\n")
            fh.write("OCG_PENDING_KEY_7=k" + "z" * 49 + "\n")   # 暂存待启用
            fh.write("OCG_PENDING_EMAIL_7=a7@example.com\n")
        with open(self.pcfg, "w", encoding="utf-8") as fh:
            json.dump({"accounts": [
                {"slot": 2, "key_var": "OCG_KEY_2", "email_var": "OCG_EMAIL_2", "enabled": False},
                {"slot": 4, "key_var": "OCG_KEY_4", "email_var": "OCG_EMAIL_4", "enabled": True},
            ]}, fh)
        conn = sqlite3.connect(self.pdb)
        conn.execute("CREATE TABLE deployments (identity TEXT, model_name TEXT, slot INTEGER)")
        conn.executemany("INSERT INTO deployments VALUES (?,?,?)",
                         [("d%d" % i, "m", 4) for i in range(3)])
        conn.commit()
        conn.close()
        self.saved = (dashboard.ENV, dashboard.POOL_CFG, dashboard.POOL_DB)
        dashboard.ENV, dashboard.POOL_CFG, dashboard.POOL_DB = self.env, self.pcfg, self.pdb

    def tearDown(self):
        import shutil
        dashboard.ENV, dashboard.POOL_CFG, dashboard.POOL_DB = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pending_and_disabled_and_inpool(self):
        rows = {r["slot"]: r for r in dashboard.account_rows()}
        self.assertEqual(set(rows), {2, 4, 7}, "三态账号必须全部被发现")
        self.assertEqual(rows[4]["state"], "inpool")        # enabled + 有 deployment
        self.assertEqual(rows[4]["deployments"], 3)
        self.assertEqual(rows[2]["state"], "disabled")      # 控制面 enabled=false
        self.assertEqual(rows[7]["state"], "pending")       # 暂存待启用
        self.assertTrue(rows[7]["pending"])
        self.assertEqual(rows[7]["key_var"], "OCG_PENDING_KEY_7")
        # 排序：inpool → disabled → pending（暂存永远排在最后，不在在池区内）
        self.assertEqual([r["slot"] for r in dashboard.account_rows()], [4, 2, 7])

    def test_pending_email_resolution_and_no_placeholder_crash(self):
        """占位邮箱分支曾写成 `"***@***" % slot` → TypeError（新增账号一进来就 500）。"""
        self.assertEqual(dashboard.account_email("7", "k", key_var="OCG_PENDING_KEY_7",
                                                 email_var="OCG_PENDING_EMAIL_7")[0],
                         "a7@example.com")
        mail, src = dashboard.account_email("99", "k" + "q" * 30)
        self.assertEqual(src, "placeholder")
        self.assertIn("@", mail)

    def test_kpi_fields_and_page_markers(self):
        page = dashboard.render_page()
        self.assertIn('id="acctPending"', page)          # 暂存待启用独立区块
        self.assertIn('id="acctSummary"', page)
        self.assertIn("不计入在池", page)
        self.assertIn("poolState", page)                 # 三态字段进了前端
        self.assertNotIn("***@***", page)                # 掩码占位已清除
        self.assertIn("历史轮换尾号", page)


class TenAccountRealShapeTest(unittest.TestCase):
    """2026-10-19（task-9）：**真实形态** —— 10 个账号全在控制面（7 在池 + 3 禁用），
    `OCG_PENDING_*` 为 0。

    背景：以前夹具是「6 张池位卡 + 4 张暂存卡」；把新账号（slot 7-10）真正启用后
    `#acctDetail` 变成 **10 张卡**，固定 3/6 列排成 3+3+3+1 → 最后一排只剩 1 张、右侧一片空
    （verifier tabs.py 报 row-wasted-right）。这里锁死「10 张卡」这个形态的数据口径与
    渲染规则（三态字段 / 前端过滤 / pending=0 时整块收起），避免以后再靠肉眼发现。
    """

    ENV_KEYS = ("POOL_HOME", "POOL_CONFIG_PATH", "POOL_DB_PATH",
                "POOL_CONFIG_YAML", "POOL_AUDIT_PATH", "POOL_RECONCILER")

    def setUp(self):
        import sqlite3
        import tempfile
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="pooltest10-")
        self.env = os.path.join(self.tmp, ".env")
        self.pcfg = os.path.join(self.tmp, "pool-config.json")
        self.pdb = os.path.join(self.tmp, "pool.db")
        # 10 个 key 全在 .env；没有 OCG_PENDING_*（真实形态：暂存账号已启用）
        with open(self.env, "w", encoding="utf-8") as fh:
            for slot in range(1, 11):
                fh.write("OCG_KEY_%d=k%s\n" % (slot, chr(96 + slot) * 49))
                fh.write("OCG_EMAIL_%d=a%d@example.com\n" % (slot, slot))
        # 控制面：1-3 禁用，4-10 启用（7 个）
        with open(self.pcfg, "w", encoding="utf-8") as fh:
            json.dump({"accounts": [
                {"slot": s, "key_var": "OCG_KEY_%d" % s,
                 "email_var": "OCG_EMAIL_%d" % s, "enabled": s >= 4}
                for s in range(1, 11)]}, fh)
        # pool.db：只有启用的 7 个槽位有 deployment（每个 3 条 → 21）
        conn = sqlite3.connect(self.pdb)
        conn.execute("CREATE TABLE deployments (identity TEXT, model_name TEXT, slot INTEGER)")
        conn.executemany("INSERT INTO deployments VALUES (?,?,?)",
                         [("s%d-d%d" % (slot, i), "m", slot)
                          for slot in range(4, 11) for i in range(3)])
        conn.commit()
        conn.close()
        self.saved = (dashboard.ENV, dashboard.POOL_CFG, dashboard.POOL_DB)
        dashboard.ENV, dashboard.POOL_CFG, dashboard.POOL_DB = self.env, self.pcfg, self.pdb

    def tearDown(self):
        import shutil
        dashboard.ENV, dashboard.POOL_CFG, dashboard.POOL_DB = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_ten_cards_seven_inpool_three_disabled(self):
        rows = dashboard.account_rows()
        self.assertEqual(len(rows), 10, "真实形态：10 个账号都要被发现")
        by_state = {}
        for r in rows:
            by_state.setdefault(r["state"], []).append(r["slot"])
        self.assertEqual(len(by_state["inpool"]), 7)
        self.assertEqual(sorted(by_state["inpool"]), [4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(sorted(by_state["disabled"]), [1, 2, 3])
        self.assertNotIn("pending", by_state, "暂存账号启用后不应再有 pending")
        # 前端 `#acctDetail` = 非 pending 行 → 恰好 10 张卡；`#acctPending` = 0 张
        self.assertEqual(len([r for r in rows if r["state"] != "pending"]), 10)
        self.assertEqual(len([r for r in rows if r["state"] == "pending"]), 0)
        # 在池卡才有 deployment（布局按 deploy 数标注「部署 N 条」）
        self.assertEqual(sum(r["deployments"] for r in rows), 21)
        self.assertTrue(all(r["deployments"] == 3 for r in rows if r["state"] == "inpool"))
        self.assertTrue(all(r["deployments"] == 0 for r in rows if r["state"] == "disabled"))

    def test_pending_section_collapses_when_zero(self):
        """pending=0 时「暂存待启用」不留空标题/空壳：前端必须把容器与标题一起 display:none。"""
        page = dashboard.render_page()
        self.assertIn('id="acctDetail"', page)
        self.assertIn('id="acctPending"', page)
        self.assertIn('id="acctPendingHd"', page)
        # 池位卡与暂存卡的分流规则（前者进 #acctDetail，后者进 #acctPending）
        self.assertIn('a.poolState!=="pending"', page)
        self.assertIn('a.poolState==="pending"', page)
        # 容器与标题都随 pendRows.length 收起（不是只藏标题留空壳）
        self.assertIn('pel.style.display=pendRows.length?"flex":"none"', page)
        self.assertIn('phd.style.display=pendRows.length?"":"none"', page)


class MonthlyTokenTest(unittest.TestCase):
    """2026-11-07：本月 Token KPI 的月界与聚合口径。"""

    def test_month_start_is_first_local_day(self):
        now = time.mktime((2026, 11, 7, 15, 30, 0, 0, 0, -1))
        start = dashboard.month_start_ms(now)
        expect = int(time.mktime((2026, 11, 1, 0, 0, 0, 0, 0, -1)) * 1000)
        self.assertEqual(start, expect)
        self.assertEqual(time.strftime("%Y-%m", time.localtime(start / 1000.0)), "2026-11")

    def test_month_usage_only_current_month(self):
        now = time.mktime((2026, 11, 7, 15, 30, 0, 0, 0, -1))
        start = dashboard.month_start_ms(now)
        rows = [
            {"ts": start - 1, "pt": 10, "ct": 20, "tt": 30},       # 上月末，排除
            {"ts": start, "pt": 1, "ct": 2, "tt": 3},              # 本月起点，计入
            {"ts": start + 3600 * 1000, "pt": 4, "ct": 5, "tt": 9},
            {"ts": "bad", "pt": 99, "ct": 99, "tt": 99},           # 坏行，跳过
            {"pt": 99, "ct": 99, "tt": 99},                        # 无 ts，跳过
        ]
        u = dashboard.month_usage(rows, now=now)
        self.assertEqual(u["hits"], 2)
        self.assertEqual(u["pt"], 5)
        self.assertEqual(u["ct"], 7)
        self.assertEqual(u["tt"], 12)
        self.assertEqual(u["since"], start)
        self.assertEqual(u["month"], "2026-11")

    def test_page_has_month_kpi(self):
        page = dashboard.render_page()
        self.assertIn('k:"mtt",n:"本月 Token"', page)
        self.assertIn("d.month_usage", page)
        self.assertIn("本月累计", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
