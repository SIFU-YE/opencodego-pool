#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply-cache-retention.py —— 落地 pi-opencode-go-cache 的三旋钮（不装插件，用 pi-ai 原生能力）

P1: cordis.patch.yml litellm profile 加 `cacheRetention: long`
    → pi-ai 每请求发 prompt_cache_key(=DSH sessionId) + prompt_cache_retention:"24h"
    → 网关缓存 TTL 5min → 24h
P2: litellm 连接内 **非 GLM** 的 mimo/deepseek 系模型加
    `compat: {cacheControlFormat: anthropic}` → cache_control 断点 ttl:"1h"
    （GLM 已知会被上游拒：Extra inputs are not permitted → 明确排除）
P3: sidecar 日志加字段存在性标记 pck/ret/cc（只记有无，不记内容）

校验：node 加载 dsh-llm-pi-ai Config 真 schema 验证解析结果；sidecar 冒烟 mimo/glm 200。
全部幂等 + 单备份；不动 litellm/不重启 DSH（cordis 走 HMR，sidecar 独立重启）。
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = "/opt/litellm"
CORDIS = "/root/.dsh/profiles/desktop/cordis.patch.yml"
SIDECAR = os.path.join(ROOT, "session-sidecar.py")
STATUS = os.path.join(ROOT, "apply-cache-retention.status.json")
TS = time.strftime("%Y%m%d-%H%M%S")
steps = []
LOG = os.path.join(ROOT, "apply-cache-retention.log")

# 非 GLM 且属 mimo / deepseek 家族（社区实测：GLM 必须排除，其余本轮只开高收益模型）
STAMP_MODELS = [
    "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-flash",
    "deepseek-v4.1-flash", "deepseek-v4-pro",
    "mimo-v2.5", "mimo-v2.5-pro", "mimo-v2.6-flash", "mimo-v2.6-pro",
]


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def step(name, ok, d=""):
    steps.append({"name": name, "ok": bool(ok), "detail": str(d)[:300]})
    log(("  OK   " if ok else "  FAIL ") + name + ((" — " + str(d)[:200]) if d else ""))
    return bool(ok)


def status(state, ok=None, reason=""):
    import json
    data = {"state": state, "ok": ok, "reason": reason, "steps": steps,
            "started": TS, "updated": time.strftime("%F %T")}
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATUS)


def sh(cmd, t=120):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 125, str(e)


def main():
    status("running")
    log("==== P1/P2/P3：retention 24h + cache_control 断点(非GLM) + 观测 ====")

    # ---------- P1 + P2: cordis.patch.yml ----------
    src = open(CORDIS, encoding="utf-8").read()
    orig = src
    s = src.index("      litellm:\n")
    e = src.index("\n      litellm-responses:", s)
    head, block, tail = src[:s], src[s:e], src[e:]

    # P1 profile 级
    if "cacheRetention: long" in block:
        log("    P1 已是目标态，跳过")
    else:
        anchor = ("      litellm:\n"
                  "        api: openai-completions\n"
                  "        apiKeyEnv: LITELLM_API_KEY\n"
                  "        baseURL: http://127.0.0.1:4001/v1\n")
        if block.count(anchor) != 1:
            step("P1 cacheRetention 锚点", False, "count=%d" % block.count(anchor))
            status("done", ok=False, reason="P1-anchor")
            return 1
        block = block.replace(anchor, anchor + "        cacheRetention: long\n", 1)
        log("    P1 已写入 cacheRetention: long")

    # P2 逐模型 compat（仅 litellm 连接块内）
    added = []
    for mid in STAMP_MODELS:
        old = "          - id: %s\n            name: %s\n" % (mid, mid)
        new = old + "            compat:\n              cacheControlFormat: anthropic\n"
        if new in block:
            continue
        if block.count(old) != 1:
            step("P2 锚点 %s" % mid, False, "count=%d" % block.count(old))
            status("done", ok=False, reason="P2-anchor:" + mid)
            return 1
        block = block.replace(old, new, 1)
        added.append(mid)
    src = head + block + tail
    glm_stamped = "glm" in "".join(added)
    step("P2 模型 compat（%d 个新增，GLM 排除=%s）" % (len(added), not glm_stamped),
         not glm_stamped, ",".join(added) or "全部已存在")

    if src != orig:
        shutil.copy2(CORDIS, CORDIS + ".bak-cache-" + TS)
        open(CORDIS, "w", encoding="utf-8").write(src)
        log("    cordis.patch.yml 已写入（备份 .bak-cache-%s）" % TS)

    # ---------- 真 schema 校验（node + dsh-llm-pi-ai Config）----------
    node_src = r"""
import {createRequire} from 'module';
const req = createRequire('/opt/dsh-desktop/DSH-NEXT-2.0.14-next-linux-x64/resources/app/index.js');
const yaml = req('js-yaml');
const fs = await import('fs');
const {Config} = await import('/opt/dsh-desktop/DSH-NEXT-2.0.14-next-linux-x64/resources/app/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js');
const src = fs.readFileSync('/root/.dsh/profiles/desktop/cordis.patch.yml','utf8').replace(/!!js\s+/g,'_js ');
const doc = yaml.load(src);
const row = doc.find(r => r && r.id === 'llm-pi-ai');
const prov = row.config.providers.litellm;
const out = Config({providers: {litellm: prov}});
const route = out.providers.get('litellm').litellm;
const pick = id => (route.models.find(m => m.id === id) || {}).compat;
console.log(JSON.stringify({
  cacheRetention: route.cacheRetention,
  mimo26f: pick('mimo-v2.6-flash'),
  dsf: pick('deepseek-v4-flash'),
  glm53: pick('glm-5.3'),
  mimo_count: route.models.filter(m => (m.compat||{}).cacheControlFormat === 'anthropic').length,
  total: route.models.length
}));
"""
    open("/tmp/validate-cache-config.mjs", "w", encoding="utf-8").write(node_src)
    rc, out = sh("node /tmp/validate-cache-config.mjs", t=60)
    if rc != 0:
        step("Config schema 校验", False, out[-400:])
        status("done", ok=False, reason="schema")
        return 1
    try:
        import json
        v = json.loads(out.strip().splitlines()[-1])
    except Exception:
        step("Config schema 校验", False, out[-300:])
        status("done", ok=False, reason="schema-parse")
        return 1
    ok = (v.get("cacheRetention") == "long"
          and (v.get("mimo26f") or {}).get("cacheControlFormat") == "anthropic"
          and (v.get("dsf") or {}).get("cacheControlFormat") == "anthropic"
          and not (v.get("glm53") or {}).get("cacheControlFormat"))
    step("真 schema 校验（retention=long / mimo+dsf 盖章 / glm 干净）", ok, json.dumps(v, ensure_ascii=False))
    if not ok:
        status("done", ok=False, reason="schema-mismatch")
        return 1

    # ---------- P3: sidecar 字段观测 + 重启 ----------
    sd = open(SIDECAR, encoding="utf-8").read()
    old_slog = '''            slog("%s %s sess=%s status=%s" % (method, self.path, sess, resp.status))'''
    new_slog = '''            flags = ""
            if method == "POST" and send_body and "chat/completions" in self.path:
                flags = " pck=%d ret=%s cc=%d" % (
                    1 if b'"prompt_cache_key"' in send_body else 0,
                    "24h" if b'"prompt_cache_retention"' in send_body else "-",
                    send_body.count(b'"cache_control"'))
            slog("%s %s sess=%s%s status=%s" % (method, self.path, sess, flags, resp.status))'''
    if "pck=%d" in sd:
        log("    P3 已是目标态，跳过")
    elif sd.count(old_slog) == 1:
        shutil.copy2(SIDECAR, SIDECAR + ".bak-cache-" + TS)
        open(SIDECAR, "w", encoding="utf-8").write(sd.replace(old_slog, new_slog, 1))
        log("    P3 sidecar 观测字段已写入")
    else:
        step("P3 sidecar 锚点", False)
        status("done", ok=False, reason="P3-anchor")
        return 1

    rc, out = sh("python3 -m py_compile %s" % SIDECAR, t=30)
    step("sidecar 语法", rc == 0, out[-120:])
    sh("systemctl restart litellm-session-sidecar", t=60)
    time.sleep(1.5)
    act = sh("systemctl is-active litellm-session-sidecar", t=20)[1].strip()
    step("sidecar 重启 active", act == "active", act)

    # ---------- 冒烟：mimo（带三旋钮）/ glm（仅 retention）均须 200 ----------
    import json as _json
    import urllib.request as _u
    key = ""
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.startswith("LITELLM_MASTER_KEY="):
            key = line.strip().split("=", 1)[1]

    def smoke(model, with_cc):
        body = {"model": model, "stream": False, "max_tokens": 16,
                "messages": [{"role": "user", "content": "smoke 1+1=? 只回答数字"}],
                "prompt_cache_key": "smoke-" + TS,
                "prompt_cache_retention": "24h"}
        if with_cc:
            body["messages"] = [{"role": "system", "content": [
                {"type": "text", "text": "你是简洁助手。",
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}]},
                {"role": "user", "content": "smoke 1+1=? 只回答数字"}]
        req = _u.Request("http://127.0.0.1:4001/v1/chat/completions",
                         data=_json.dumps(body).encode(),
                         headers={"content-type": "application/json",
                                  "Authorization": "Bearer " + key})
        d = _json.loads(_u.urlopen(req, timeout=120).read())
        return "choices" in d

    try:
        step("冒烟 mimo-v2.6-flash（retention+key+cache_control 经全链路）",
             smoke("mimo-v2.6-flash", True))
    except Exception as ex:
        step("冒烟 mimo-v2.6-flash", False, str(ex)[:160])
    try:
        step("冒烟 glm-5.3（仅 retention，无 cache_control）", smoke("glm-5.3", False))
    except Exception as ex:
        step("冒烟 glm-5.3", False, str(ex)[:160])

    bad = [s for s in steps if not s["ok"]]
    if bad:
        status("done", ok=False, reason="failed: " + bad[0]["name"])
        return 1
    log("==== 完成：P1/P2/P3 全部就位")
    status("done", ok=True, reason="all-checks-passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        log("执行器异常：%s" % e)
        status("done", ok=False, reason="exception: " + str(e)[:200])
        code = 1
    log("退出码 %s" % code)
    sys.exit(code)
