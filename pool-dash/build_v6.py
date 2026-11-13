# -*- coding: utf-8 -*-
"""从 v5 dashboard.py 构建 v6：回归 Agent Teams 团队规则编辑器（号池内化）"""
import io, sys

SRC = "dashboard.py"
src = io.open(SRC, encoding="utf-8").read()

# 1) docstring -> v6
old_doc = '''"""OpenCode Go 号池看板 v5：号池职责收敛为纯负载均衡代理与用量观测
- v3：模型选择（按 API 类型分类）+ 一键同步 DSH 配置
- v4：（已移除）agent-teams 团队规则编辑器 → 迁至独立服务 /opt/dsh-teams/teams.py
      （/works/dsh-teams/，属于 DSH 配置侧而非号池）
- v5：移除团队规则整块；号池不再提供 pinned 单账号别名（PINNED_ALIASES = {}）'''
new_doc = '''"""OpenCode Go 号池看板 v6：团队规则编辑器回归号池（用户指示，DSH 内嵌方案受阻）
- v3：模型选择（按 API 类型分类）+ 一键同步 DSH 配置
- v5：移除团队规则整块 + 号池去 pinned（PINNED_ALIASES = {}）
- v6：恢复「Agent Teams · 团队模型规则」编辑器（memberModel/maxMembers/depth/profiles），
      独立服务 /opt/dsh-teams/ 与 /works/dsh-teams/ 已下线，本页为唯一入口'''
assert old_doc in src, "docstring anchor missing"
src = src.replace(old_doc, new_doc, 1)

# 2) backend block
SUMMARY = "# ---------- 汇总 ----------"
assert SUMMARY in src
backend = '''# ---------- Agent Teams 团队模型规则 ----------

TEAMS_ANCHOR = re.compile(r"- id: agent-teams\\n")

def teams_config():
    """读取 cordis.patch.yml 中 agent-teams 条目（memberModel/maxMembers/profiles）"""
    base = {"memberModel": "", "memberMaxDepth": 0, "maxMembers": 8, "profiles": {}}
    try:
        src = open(DSH_PATCH).read()
    except Exception as e:
        return {"available": pool_models(), "config": base, "present": False, "error": str(e)[:120]}
    m = TEAMS_ANCHOR.search(src)
    if not m:
        return {"available": pool_models(), "config": base, "present": False}
    try:
        import yaml
        d = yaml.safe_load(src[m.start():].replace("!!js", "_js"))
        entry = d[0] if isinstance(d, list) else d
        cfg = (entry or {}).get("config", {}) if isinstance(entry, dict) else {}
    except Exception as e:
        return {"available": pool_models(), "config": base, "present": True, "error": "解析失败: %s" % str(e)[:120]}
    base["memberModel"] = cfg.get("memberModel", "")
    base["memberMaxDepth"] = cfg.get("memberMaxDepth", 0)
    base["maxMembers"] = cfg.get("maxMembers", 8)
    profs = {}
    for name, p in (cfg.get("profiles") or {}).items():
        if not isinstance(p, dict):
            continue
        members = [{"name": mm.get("name", ""), "role": mm.get("role", "teammate"),
                    "model": mm.get("model", "")}
                   for mm in (p.get("members") or []) if isinstance(mm, dict)]
        profs[name] = {"description": p.get("description", ""), "members": members}
    base["profiles"] = profs
    return {"available": pool_models(), "config": base, "present": True}

SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
SAFE_ROLE = re.compile(r"^[A-Za-z0-9_\\u4e00-\\u9fff -]{0,64}$")

def _yaml_str(s):
    return '"%s"' % str(s).replace("\\\\", "\\\\\\\\").replace('"', '\\\\"')

def save_teams_config(cfg, restart):
    """校验并重写 agent-teams 块（条目至文件尾），YAML 校验失败回滚；可选重启 dsh"""
    member_model = str(cfg.get("memberModel") or "").strip()
    if not SAFE_NAME.match(member_model):
        return {"ok": False, "error": "memberModel 非法: %s" % member_model[:40]}
    try:
        max_members = int(cfg.get("maxMembers", 8))
        max_depth = int(cfg.get("memberMaxDepth", 0))
    except Exception:
        return {"ok": False, "error": "maxMembers / memberMaxDepth 必须是整数"}
    if not (1 <= max_members <= 64):
        return {"ok": False, "error": "maxMembers 需在 1-64 之间"}
    if not (0 <= max_depth <= 3):
        return {"ok": False, "error": "memberMaxDepth 需在 0-3 之间"}
    profs_in = cfg.get("profiles") or {}
    if len(profs_in) > 16:
        return {"ok": False, "error": "profiles 最多 16 个模板"}
    valid_models = set(pool_models())
    parsed = []
    for pname, p in profs_in.items():
        if not SAFE_NAME.match(str(pname or "")):
            return {"ok": False, "error": "模板名称非法: %s（限字母/数字/_-.，且不以-开头）" % pname}
        members = p.get("members") or []
        if not (1 <= len(members) <= 32):
            return {"ok": False, "error": "模板 %s 成员数需在 1-32 之间" % pname}
        rows = []
        for mm in members:
            name = str((mm or {}).get("name") or "").strip()
            role = str((mm or {}).get("role") or "").strip()
            model = str((mm or {}).get("model") or "").strip()
            if not SAFE_NAME.match(name):
                return {"ok": False, "error": "成员名非法: %s" % name[:40]}
            if not SAFE_ROLE.match(role):
                return {"ok": False, "error": "成员 %s 的角色非法" % name}
            if model not in valid_models:
                return {"ok": False, "error": "成员 %s 的模型不在号池清单中: %s" % (name, model[:40])}
            rows.append((name, role or "teammate", model))
        parsed.append((pname, str((p or {}).get("description") or "").strip(), rows))

    out = ["# 由号池看板「团队模型规则」页管理：保存即重写本块（自动备份 + YAML 校验）。",
           "- id: agent-teams",
           "  config:",
           "    stateDir: .agent-teams",
           "    memberProvider: spawn",
           "    memberModel: %s" % member_model,
           "    memberMaxDepth: %d" % max_depth,
           "    maxMembers: %d" % max_members]
    if parsed:
        out.append("    profiles:")
        for pname, desc, rows in parsed:
            out.append("      %s:" % pname)
            out.append("        description: %s" % _yaml_str(desc or ("团队模板 " + pname)))
            out.append("        taskPlanning: captain")
            out.append("        members:")
            for name, role, model in rows:
                out.append("          - name: %s" % name)
                out.append("            role: %s" % _yaml_str(role))
                out.append("            provider: litellm")
                out.append("            model: %s" % model)
    new_block = "\\n".join(out) + "\\n"

    try:
        src = open(DSH_PATCH).read()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    backup = DSH_PATCH + ".bak-teams-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DSH_PATCH, backup)
    m = TEAMS_ANCHOR.search(src)
    if m:
        # 只替换 agent-teams 块本身（到下一个顶层 "- id: " 为止），保留下游条目
        nxt = re.search(r"(?m)^- id: ", src[m.end():])
        blk_end = (m.end() + nxt.start()) if nxt else len(src)
        new_src = src[:m.start()] + new_block + (("\\n" + src[blk_end:]) if blk_end < len(src) else "")
    else:
        new_src = src.rstrip("\\n") + "\\n\\n" + new_block
    try:
        import yaml
        yaml.safe_load(new_src.replace("!!js", "_js"))
    except Exception as e:
        shutil.copy2(backup, DSH_PATCH)
        return {"ok": False, "error": "YAML 校验失败已回滚: %s" % str(e)[:120], "backup": backup}
    open(DSH_PATCH, "w").write(new_src)
    r = {"ok": True, "backup": backup, "profiles": len(parsed)}
    if restart:
        r.update(restart_dsh())
    return r

'''
src = src.replace(SUMMARY, backend + SUMMARY, 1)

# 3) HTML teams section after 状态码分布
STATUS_H2 = '<h2>状态码分布</h2><div class="chips" id="status"></div>'
assert STATUS_H2 in src
html_block = STATUS_H2 + '''
<h2>Agent Teams · 团队模型规则 <span style="text-transform:none;letter-spacing:0" class="sub">· 队长模型由 dsh 会话模型选择器决定；成员默认 memberModel + 模板按角色固定</span></h2>
<div class="prov" id="teamsScalar">
 <div class="ph"><span class="pname">全局成员默认</span>
  <label style="display:flex;align-items:center;gap:6px;font-size:13px">memberModel
   <select id="tcMM" style="background:#1e2430;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 8px"></select></label>
  <label style="display:flex;align-items:center;gap:6px;font-size:13px">maxMembers
   <input id="tcMX" type="number" min="1" max="64" style="width:64px;background:#1e2430;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 6px"></label>
  <label style="display:flex;align-items:center;gap:6px;font-size:13px">memberMaxDepth
   <input id="tcMD" type="number" min="0" max="3" style="width:64px;background:#1e2430;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 6px"></label>
 </div>
</div>
<div id="teamsBox"></div>
<div class="syncrow">
 <button id="teamAddProf">+ 新增模板</button>
 <button id="teamsSave" style="background:#1d4ed8;border-color:#2563eb;color:#fff;font-size:14px;padding:8px 22px;font-weight:600">保存团队规则并重启服务</button>
 <span class="sub">保存即重写 cordis.patch.yml 的 agent-teams 块（自动备份 + YAML 校验）→ 重启 dsh-desktop 生效；模板用 /agent-teams --profile <b>名称</b> 目标 调用</span>
 <span id="teamsMsg"></span>
</div>'''
src = src.replace(STATUS_H2, html_block, 1)

# 4) JS block + loadTeams() call
LOAD_LINE = "load();setInterval(load,15000);loadProv();"
assert LOAD_LINE in src
js_block = '''// ---------- Agent Teams 团队模型规则 ----------
let MODELS=[];
function opts(cur){return MODELS.map(m=>`<option value="${esc(m)}" ${m===cur?"selected":""}>${esc(m)}</option>`).join("")}
function memberRow(mm){
  return `<div class="ph" data-tc="member" style="gap:6px;margin:4px 0">
    <input class="tc-mname" placeholder="成员名" value="${esc(mm.name||"")}" style="width:110px;background:#1e2430;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 8px">
    <input class="tc-mrole" placeholder="角色 role" value="${esc(mm.role||"teammate")}" style="width:120px;background:#1e2430;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 8px">
    <select class="tc-mmodel" style="background:#1e2430;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 8px">${opts(mm.model||MODELS[0]||"")}</select>
    <button onclick="this.parentNode.remove()">删除成员</button></div>`;
}
function profBox(name,p){
  const members=(p&&p.members||[]).map(memberRow).join("");
  return `<div class="prov" data-tc="prof" data-name="${esc(name)}">
    <div class="ph"><span class="pname">模板 ${esc(name)}</span>
      <span class="sub">/agent-teams --profile ${esc(name)}</span>
      <span class="pact"><button onclick="tcDelProf('${esc(name)}')">删除模板</button></span></div>
    <div style="margin:4px 0"><span class="sub">描述：</span><input class="tc-pdesc" value="${esc((p&&p.description||"").replace(/"/g,"&quot;"))}" style="width:60%;background:#1e2430;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 8px"></div>
    <div class="tc-members">${members}</div>
    <button onclick="tcAddMember(this)">+ 添加成员</button></div>`;
}
function renderTeams(){
  if(!TC)return;
  document.getElementById("tcMM").innerHTML=opts(TC.memberModel||"");
  document.getElementById("tcMX").value=TC.maxMembers??8;
  document.getElementById("tcMD").value=TC.memberMaxDepth??0;
  document.getElementById("teamsBox").innerHTML=Object.entries(TC.profiles||{}).map(([n,p])=>profBox(n,p)).join("")
    ||'<div class="sub">暂无模板 —— 点击「+ 新增模板」创建</div>';
}
function tcAddMember(btn){
  btn.previousElementSibling.insertAdjacentHTML("beforeend",
    memberRow({name:"member"+(Math.floor(Math.random()*90)+10),role:"teammate",model:MODELS[0]||""}));
}
function tcDelProf(name){delete TC.profiles[name];renderTeams()}
async function loadTeams(){
  try{
    const d=await (await fetch("api/teams-config",{cache:"no-store"})).json();
    MODELS=d.available||[];TC=d.config||{};
    if(!TC.profiles)TC.profiles={};
    renderTeams();
  }catch(e){document.getElementById("teamsMsg").textContent="团队配置加载失败: "+e}
}
document.getElementById("teamAddProf").addEventListener("click",()=>{
  const name=prompt("模板名称（/agent-teams --profile 调用时使用，限字母数字_-）");
  if(!name)return;
  if(!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/.test(name)){alert("名称非法");return}
  if(TC.profiles[name]){alert("模板已存在");return}
  TC.profiles[name]={description:"团队模板 "+name,members:[{name:"worker",role:"teammate",model:MODELS[0]||""}]};
  renderTeams();
});
document.getElementById("teamsSave").addEventListener("click",async ()=>{
  const btn=document.getElementById("teamsSave"),msg=document.getElementById("teamsMsg");
  btn.disabled=true;msg.textContent="正在保存并重启 dsh-desktop…";msg.className="sub";
  const profiles={};
  document.querySelectorAll('#teamsBox [data-tc="prof"]').forEach(box=>{
    const name=box.dataset.name;
    const members=[...box.querySelectorAll('[data-tc="member"]')].map(r=>({
      name:r.querySelector(".tc-mname").value.trim(),
      role:r.querySelector(".tc-mrole").value.trim(),
      model:r.querySelector(".tc-mmodel").value}));
    profiles[name]={description:box.querySelector(".tc-pdesc").value,members};
  });
  const payload={config:{
    memberModel:document.getElementById("tcMM").value,
    maxMembers:parseInt(document.getElementById("tcMX").value)||8,
    memberMaxDepth:parseInt(document.getElementById("tcMD").value)||0,
    profiles},restart:true};
  try{
    const r=await (await fetch("api/save-teams-config",{method:"POST",cache:"no-store",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})).json();
    if(r.ok){
      msg.textContent=`✔ 已保存（模板 ${r.profiles} 个）· 服务 ${r.service||"已重启"} · 备份 ${r.backup}`;
      msg.className="ok";
    }else{msg.textContent="✘ 保存失败: "+(r.error||"未知错误");msg.className="err";}
  }catch(e){msg.textContent="✘ 请求失败: "+e;msg.className="err";}
  btn.disabled=false;
});
loadTeams();
''' + LOAD_LINE
src = src.replace(LOAD_LINE, js_block, 1)

# 5) routes
GET_ANCHOR = '        elif self.path == "/api/models-config":'
assert GET_ANCHOR in src
get_new = '''        elif self.path == "/api/teams-config":
            try:
                self._send(200, json.dumps(teams_config(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)[:200]}), "application/json; charset=utf-8")
''' + GET_ANCHOR
src = src.replace(GET_ANCHOR, get_new, 1)

# do_POST 尾部的 else(404) 前插入新路由；用 sync-models 的 except 块做唯一锚
# （do_GET 的 except 是 {"error":...}，不含 "ok": False，因此此锚唯一）
POST_ANCHOR = '''            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}), "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")'''
assert POST_ANCHOR in src
post_new = '''            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}), "application/json; charset=utf-8")
        if self.path == "/api/save-teams-config":
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln) or b"{}")
                r = save_teams_config(body.get("config") or {}, bool(body.get("restart")))
                self._send(200, json.dumps(r, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}), "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")'''
src = src.replace(POST_ANCHOR, post_new, 1)

io.open(SRC, "w", encoding="utf-8", newline="\n").write(src)
print("v6 built OK, lines:", src.count("\n"))
