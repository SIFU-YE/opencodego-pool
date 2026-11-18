#!/usr/bin/env python3
"""OpencodeLitellmPool 管理控制台「页签片段」（内嵌在号池看板内）。

2026-10-17 改版：管理控制台不再是独立页面 `/admin`，而是号池看板的一个页签。
本模块因此只产出一段 **可嵌入的 HTML 片段**（`build_panel`），由 `dashboard.py`
的 `render_page()` 注入到看板骨架里；页签样式/主题全部复用看板的 CSS 变量。

纯 HTML + 原生 JS，零前端构建、零新增依赖。片段本身不含任何业务数据：
所有列表/预览/执行都走同源 `/api/admin/*`；本模块只负责骨架与交互。

安全约定（与 pool_admin.py 一致）：
- 服务端注入的动态值（降级横幅文案、bootstrap JSON）都做 HTML/JSON 转义：
  横幅用 ``html.escape(..., quote=True)``；JSON 用 ``json.dumps`` 后把 ``<`` 转成
  ``\\u003c``，避免 ``</script>`` 提前闭合。
- 页面 JS 侧所有动态字符串一律经过 ``esc()``（含引号）再进 innerHTML。
- 完整密钥永不进入页面数据：实例键只在签发响应里出现一次（由 JS 直接放进
  ``textContent``，不经 innerHTML）；账号密钥只显示指纹。
- 整段 JS 包在 IIFE 里，只对外暴露 ``window.adminInit``：既避免 ``esc``/``$``
  与看板主脚本的同名全局函数互相覆盖，也让管理面在各接口（含
  ``reconciler --check`` 子进程）上保持**懒初始化** —— 不打开页签就不发请求。
"""
import html
import json

PROJECT_FALLBACK = "OpencodeLitellmPool"

__all__ = ["build_panel", "esc"]


def esc(value):
    """HTML 转义（含单双引号），供服务端拼少量静态片段时使用。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _banner(state):
    """降级/就绪横幅。文案是服务端动态值 → 必须转义。"""
    if state.get("readonly"):
        reason = state.get("reason") or "管理模块不可用"
        return ('<div class="banner bad"><b>只读模式</b> · ' + esc(reason)
                + '。主看板不受影响；修复上述依赖后刷新本页。</div>')
    if state.get("uninitialized"):
        return ('<div class="banner warn"><b>控制面未初始化</b> · 未找到 '
                + esc(state.get("config_path") or "pool-config.json")
                + '。可先用本页"预览→确认"写入初始 allowlist/账号（写入即创建控制文件）。</div>')
    return ('<div class="banner ok">管理模块就绪 · 模型/账号写入经 reconciler 热应用'
            '（sidecar ≤2s、reconciler ≤60s，不重启服务）；实例键/钉号模式秒级生效。</div>')


def build_panel(state):
    """生成管理控制台页签片段。state 见 pool_admin.AdminApp.state()。"""
    state = dict(state or {})
    project = state.get("project") or PROJECT_FALLBACK
    boot = {
        "project": project,
        "home": state.get("home") or "",
        "readonly": bool(state.get("readonly")),
        "uninitialized": bool(state.get("uninitialized")),
        "controlOk": bool(state.get("control_ok")),
        "reconcilerOk": bool(state.get("reconciler_ok")),
        "reason": state.get("reason") or "",
        "affinityMode": state.get("affinity_mode") or "shadow",
        "capabilities": state.get("capabilities") or {},
    }
    boot_json = json.dumps(boot, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    banner = _banner(state)
    return (_PANEL
            .replace("__BANNER__", banner)
            .replace("__BOOT_JSON__", boot_json)
            .replace("__PROJECT__", esc(project)))


# 片段：不含 <html>/<head>/<style>；类名复用看板 CSS（.sc/.chip/.badge/.btn/.banner/...）。
_PANEL = r"""<div class="adm" id="adminPanel">
<div class="adm-hd">
  <span class="adm-title">__PROJECT__ · 管理控制台</span>
  <span class="adm-note">本机管理面 · <b>无登录鉴权</b>：默认只监听 <span class="mono">127.0.0.1</span>；
    远程访问请自备反向代理 + 鉴权/VPN，反代访问时需把对外域名加入
    <span class="mono">POOL_ADMIN_HOSTS</span>（否则 Host 校验会 403）。</span>
</div>
__BANNER__
<div class="chips" id="capChips"></div>

<div class="adm-grid">
  <section class="sc">
    <h2>模型 allowlist<span class="h2sub">开放给客户端的模型</span></h2>
    <div class="hint" id="modelsHint">加载中…</div>
    <div id="modelsBox" class="grid" style="margin:10px 0">加载中…</div>
    <div class="row"><button id="modelsRefresh" class="btn">刷新</button>
      <button id="modelsPreview" class="btn primary">预览变更</button>
      <span class="hint" id="modelsMsg"></span></div>
    <div class="hint">勾选后先预览 diff 与影响面，再输入确认串执行。写控制面文件后 sidecar ≤2s 生效，
      reconciler ≤60s 热应用（不重启服务）。候选模型 = 控制面 allowlist ∪ 已部署
      （<span class="mono">pool.db</span> 的 <span class="mono">deployments</span> 表）∪ 可选
      <span class="mono">model-catalog.json</span> ∪ <span class="mono">config.yaml</span> 及其历史备份；
      "已部署"= <span class="mono">pool.db</span> 的 <span class="mono">deployments</span> 表实况
      （读不到时回退 <span class="mono">config.yaml</span>），下线后仍可重新勾选回来。</div>
  </section>

  <section class="sc">
    <h2>上游账号<span class="h2sub">槽位 / 邮箱 / 指纹 / 启用状态</span></h2>
    <div class="tblwrap"><table><thead><tr><th>槽号</th><th>key 变量</th><th>邮箱</th><th>key 指纹</th><th>状态</th><th>操作</th></tr></thead>
      <tbody id="acctTbl"><tr><td colspan="6" class="hint">加载中…</td></tr></tbody></table></div>
    <div class="row" style="margin-top:12px">
      <input id="newSlot" class="inp" type="number" min="1" max="999" placeholder="槽号 1-999" style="width:130px">
      <input id="newEmail" class="inp" type="email" placeholder="邮箱（可选）" style="width:200px">
      <input id="newKey" class="inp" type="password" placeholder="上游 key（新增必填；轮换时填）" autocomplete="off" style="width:290px">
      <button id="acctAdd" class="btn primary">新增 / 轮换 → 预览</button>
      <span class="hint" id="acctMsg"></span>
    </div>
    <div class="hint">槽号单调分配、删除后保留空洞、<b>绝不复用</b>（槽号参与会话身份与前缀缓存）。
      密钥只写 <span class="mono">.env</span>，控制面文件只存变量名；页面/接口/审计永不回显完整 key，只显示指纹。</div>
  </section>

  <section class="sc">
    <h2>客户端实例键<span class="h2sub">sk-pool-*</span></h2>
    <div class="tblwrap"><table><thead><tr><th>label</th><th>尾号</th><th title="本轮不采集；如需可后续用离线聚合实现">最后使用 ⓘ</th><th>操作</th></tr></thead>
      <tbody id="keyTbl"><tr><td colspan="4" class="hint">加载中…</td></tr></tbody></table></div>
    <div class="row" style="margin-top:12px">
      <input id="newLabel" class="inp" maxlength="32" placeholder="label（小写字母/数字/-，≤32）" style="width:240px">
      <button id="keyIssue" class="btn primary">签发 → 预览</button>
      <span class="hint" id="keyMsg"></span>
    </div>
    <div id="oneKeyBox" hidden><b>完整实例键（仅显示这一次，请立即复制保存）：</b>
      <code id="oneKey"></code>
      <button id="copyKey" class="btn">复制</button><button id="closeKey" class="btn ghost">我已保存</button></div>
    <div class="hint">即时生效：sidecar 每请求重读 <span class="mono">dsh-instance-keys.json</span>，吊销后下一个请求即 401。
      "最后使用" sidecar 未落盘时显示 —。</div>
  </section>

  <section class="sc">
    <h2>钉号模式<span class="h2sub">会话树 affinity</span></h2>
    <div class="row">
      <label class="opt"><input type="radio" name="affmode" value="shadow" id="affShadow"> shadow（影子：只记录，不改变路由）</label>
      <label class="opt"><input type="radio" name="affmode" value="full" id="affFull"> full（启用钉号：同一会话树固定同一账号）</label>
    </div>
    <div class="row"><button id="affPreview" class="btn primary">预览切换</button><span class="hint" id="affMsg"></span></div>
    <div class="hint" id="affHint">当前：— · sidecar 5s 热读，无需重启</div>
  </section>

  <section class="sc adm-wide">
    <h2>审计<span class="h2sub">最近写操作（脱敏）</span></h2>
    <div class="row"><button id="auditRefresh" class="btn">刷新</button><span class="hint" id="auditMsg"></span></div>
    <div class="tblwrap"><table><thead><tr><th>时间</th><th>操作</th><th>对象</th><th>结果</th></tr></thead>
      <tbody id="auditTbl"><tr><td colspan="4" class="hint">加载中…</td></tr></tbody></table></div>
  </section>
</div>

<div id="confirmPanel" class="confirm" hidden>
  <div class="cf-head"><b id="cfTitle">确认操作</b><button id="cfCancel" class="btn ghost">取消</button></div>
  <div class="cf-label">影响面</div>
  <pre id="cfImpact"></pre>
  <div class="cf-label" id="cfDiffLabel" hidden>diff</div>
  <pre id="cfDiff" hidden></pre>
  <div class="cf-label" id="cfHint"></div>
  <div class="row">
    <input id="cfInput" class="inp" type="text" placeholder="输入确认串" autocomplete="off" spellcheck="false" style="width:320px">
    <button id="cfConfirm" class="btn danger" disabled>确认执行</button>
  </div>
  <label class="row opt" id="cfAckWrap" hidden><input type="checkbox" id="cfAck">
    我已知晓：热路径不可用，本次执行将降级为 gen_config + docker restart（较慢，需要额外确认）</label>
  <div class="hint" id="cfExtra"></div>
</div>

<div id="jobBox" class="sc" hidden><div class="cf-label">热应用 Job</div><div id="jobSteps"></div></div>

<script id="adminBoot" type="application/json">__BOOT_JSON__</script>
<script>
/* 管理控制台页签脚本：整段 IIFE，只暴露 window.adminInit（懒初始化）。 */
(function(){
"use strict";
var BOOT = JSON.parse(document.getElementById("adminBoot").textContent || "{}");
function $(id){ return document.getElementById(id); }
function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
    return ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c];
  });
}
var TOAST_T = null;
function toast(msg, kind){
  var t = $("toast");
  if(!t){ t = document.createElement("div"); t.id="toast"; document.body.appendChild(t); }
  t.textContent = String(msg == null ? "" : msg);
  t.className = "toast " + (kind || "ok");
  t.hidden = false;
  if(TOAST_T) clearTimeout(TOAST_T);
  TOAST_T = setTimeout(function(){ t.hidden = true; }, 8000);
}
async function api(path, body){
  var opt = {cache: "no-store"};
  if(body !== undefined){
    opt.method = "POST";
    opt.headers = {"Content-Type": "application/json"};
    opt.body = JSON.stringify(body);
  }
  var r, j;
  try { r = await fetch("api/admin/" + path, opt); }   // 相对路径：经 nginx 前缀（/works/pool/）访问时同样成立
  catch(e){ throw new Error("网络请求失败: " + e); }
  try { j = await r.json(); }
  catch(e){ throw new Error("响应不是 JSON（HTTP " + r.status + "）"); }
  if(!j || j.ok !== true){
    throw new Error((j && j.error && j.error.message) ? j.error.message : ("HTTP " + r.status));
  }
  return j.data;
}

/* ---------------- 确认面板（四段式：预览 → 输入确认串 → 执行 → 结果） ---------------- */
var PENDING = null;
function startConfirm(pv){
  if(!pv || !pv.token){ toast("预览响应缺少 token", "err"); return; }
  PENDING = pv;
  $("cfTitle").textContent = pv.title || "确认操作";
  $("cfImpact").textContent = (pv.impact || []).join("\n");
  var det = pv.details || [];
  $("cfDiff").textContent = det.join("\n");
  $("cfDiffLabel").hidden = (det.length === 0);
  $("cfDiff").hidden = (det.length === 0);
  $("cfHint").textContent = "请输入确认串： " + (pv.confirmHint || pv.confirmExpected || "");
  $("cfExtra").textContent = pv.extra || "";
  var inp = $("cfInput");
  inp.value = "";
  inp.dataset.expect = String(pv.confirmExpected || "");
  var ack = $("cfAck");
  ack.checked = false;
  $("cfAckWrap").hidden = !pv.ackRequired;
  updateConfirmBtn();
  $("confirmPanel").hidden = false;
  $("confirmPanel").scrollIntoView({behavior: "smooth", block: "nearest"});
}
function updateConfirmBtn(){
  if(!PENDING){ $("cfConfirm").disabled = true; return; }
  var ok = (($("cfInput").value || "").trim() === String(PENDING.confirmExpected));
  if(PENDING.ackRequired){ ok = ok && $("cfAck").checked; }
  $("cfConfirm").disabled = !ok;
}

/* ---------------- 秒级结果（实例键 / 钉号模式） ---------------- */
function handleInstant(data){
  if(!data){ toast("已执行", "ok"); return; }
  if(data.key){
    $("oneKey").textContent = data.key;
    $("oneKeyBox").hidden = false;
    toast("实例键已签发（完整键只显示这一次）", "ok");
    return;
  }
  if(data.revoked){ toast("实例键已吊销: " + (data.label || ""), "ok"); return; }
  if(data.mode){ toast("钉号模式已切换为 " + data.mode, "ok"); return; }
  toast("已执行", "ok");
}

/* ---------------- Job 轮询（模型 / 账号热应用） ---------------- */
async function showJob(jobId, after){
  var box = $("jobBox"), steps = $("jobSteps");
  box.hidden = false;
  for(var i = 0; i < 300; i++){
    var j;
    try { j = await api("jobs/" + encodeURIComponent(jobId)); }
    catch(e){ toast("Job 查询失败: " + e.message, "err"); return; }
    var list = (j.steps || []).map(function(s){
      return '<div class="step ' + esc(s.state) + '">[' + esc(s.state) + '] ' + esc(s.name)
           + (s.detail ? " · " + esc(s.detail) : "") + '</div>';
    }).join("");
    var out = j.output ? '<pre style="margin-top:8px">' + esc(j.output) + '</pre>' : "";
    var state = j.state === "done" ? "ok" : (j.state === "error" ? "err" : "");
    steps.innerHTML = '<div class="hint ' + state + '">Job ' + esc(j.id) + ' · ' + esc(j.state)
             + (j.error ? " · " + esc(j.error) : "") + '</div>' + list + out;
    if(j.state === "done"){
      toast("执行完成", "ok");
      if(after){ after(); }
      return;
    }
    if(j.state === "error"){
      toast("执行失败: " + (j.error || "见 Job 步骤"), "err");
      if(after){ after(); }
      return;
    }
    await new Promise(function(res){ setTimeout(res, 1000); });
  }
  toast("Job 超时，请稍后刷新查看", "err");
}

/* ---------------- 模型 allowlist ---------------- */
async function loadModels(){
  var box = $("modelsBox"), msg = $("modelsMsg"), hint = $("modelsHint");
  try {
    var d = await api("models");
    var allow = d.allow || [];
    var allowSet = {};
    allow.forEach(function(m){ allowSet[m] = true; });
    var deployedSet = {};
    (d.deployed || []).forEach(function(m){ deployedSet[m] = true; });
    var avail = d.available || [];
    box.innerHTML = avail.map(function(m){
      var tag = deployedSet[m] ? "已部署" : "未部署";
      return '<label><input type="checkbox" value="' + esc(m) + '"' + (allowSet[m] ? " checked" : "")
           + '> <span class="mono" style="font-size:12px">' + esc(m) + '</span>'
           + '<span class="tag">' + esc(tag) + '</span></label>';
    }).join("") || '<div class="hint">没有可管理的模型（上游清单不可用）</div>';
    // 2026-10-19（task-5）：实况在前 —— deployedRows = pool.db deployments 行数（与主看板 KPI
    // 「部署」同源同值）；allowlist × 启用账号 只是旧估算口径，显式标注，避免两个数字互相矛盾。
    hint.textContent = "allowlist " + allow.length + " 个 · 已部署 " + (d.deployedRows == null ? "—" : d.deployedRows)
      + " 条 deployment（" + ((d.deployedModels || []).length) + " 个模型） · 启用账号 "
      + (d.accountCount == null ? "—" : d.accountCount) + " 个 · 估算 "
      + (d.deploymentsEstimated == null ? "—" : d.deploymentsEstimated) + " 个（= allowlist × 启用账号，仅供参考）";
    msg.textContent = d.readonly ? "当前只读：写入依赖不可用" : "";
  } catch(e){
    box.innerHTML = '<div class="err">加载失败: ' + esc(e.message) + '</div>';
    hint.textContent = "";
  }
}

/* ---------------- 账号 ---------------- */
async function loadAccounts(){
  var tb = $("acctTbl"), msg = $("acctMsg");
  try {
    var d = await api("accounts");
    var rows = (d.accounts || []).map(function(a){
      var dis = BOOT.readonly ? " disabled" : "";
      var st = a.enabled ? '<span class="badge on">启用</span>' : '<span class="badge off">禁用</span>';
      return "<tr><td>a" + esc(a.slot) + "</td>"
        + '<td class="mono">' + esc(a.key_var || "") + '</td>'
        + '<td class="mono">' + esc(a.email || a.email_var || "—") + '</td>'
        + '<td class="fp">' + esc(a.fingerprint || (a.keyPresent ? "（有值）" : "（缺失）")) + '</td>'
        + "<td>" + st + "</td>"
        + '<td class="acts"><button class="btn" data-act="rotate" data-slot="' + esc(a.slot) + '"' + dis + ">轮换 key</button>"
        + '<button class="btn" data-act="toggle" data-slot="' + esc(a.slot) + '"' + dis + ">" + (a.enabled ? "禁用" : "启用") + "</button>"
        + '<button class="btn" data-act="delete" data-slot="' + esc(a.slot) + '"' + dis + ">删除</button></td></tr>";
    }).join("");
    tb.innerHTML = rows || '<tr><td colspan="6" class="hint">尚无账号（用下方表单新增；槽号从 a' + esc(d.nextSlot || 1) + ' 开始）</td></tr>';
    msg.textContent = "共 " + (d.accounts || []).length + " 个账号 · 启用 " + (d.enabledCount == null ? "—" : d.enabledCount)
      + " 个 · 下一个可用槽号 a" + (d.nextSlot == null ? "—" : d.nextSlot);
  } catch(e){
    tb.innerHTML = '<tr><td colspan="6" class="err">加载失败: ' + esc(e.message) + '</td></tr>';
  }
}
async function previewAccounts(payload){
  try {
    var pv = await api("accounts/preview", payload);
    pv.applyPath = "accounts/apply";
    pv.after = loadAccounts;
    startConfirm(pv);
    $("acctMsg").textContent = "已生成预览，请查看下方确认面板";
  } catch(e){ toast("预览失败: " + e.message, "err"); }
}

/* ---------------- 实例键 ---------------- */
async function loadKeys(){
  var tb = $("keyTbl");
  try {
    var d = await api("instance-keys");
    var rows = (d.keys || []).map(function(k){
      var dis = BOOT.readonly ? " disabled" : "";
      return "<tr><td>" + esc(k.label) + "</td>"
        + '<td class="fp">…' + esc(k.tail || "") + "</td>"
        + '<td class="hint">' + esc(k.lastUsed || "—") + "</td>"
        + '<td class="acts"><button class="btn" data-revoke="' + esc(k.label) + '"' + dis + ">吊销</button></td></tr>";
    }).join("");
    tb.innerHTML = rows || '<tr><td colspan="4" class="hint">尚未签发实例键</td></tr>';
  } catch(e){
    tb.innerHTML = '<tr><td colspan="4" class="err">加载失败: ' + esc(e.message) + '</td></tr>';
  }
}

/* ---------------- 钉号模式 ---------------- */
async function loadAffinity(){
  try {
    var d = await api("affinity");
    var mode = d.mode || "shadow";
    if($("aff" + (mode === "full" ? "Full" : "Shadow"))){ $("aff" + (mode === "full" ? "Full" : "Shadow")).checked = true; }
    $("affHint").textContent = "当前：" + mode + "（控制面 " + (d.controlMode || "—")
      + " / sidecar 镜像 " + (d.mirrorMode || "—") + "）· sidecar 5s 热读，无需重启";
  } catch(e){
    $("affHint").textContent = "加载失败: " + e.message;
  }
}

/* ---------------- 审计 ---------------- */
async function loadAudit(){
  var tb = $("auditTbl"), msg = $("auditMsg");
  try {
    var d = await api("audit?limit=50");
    var rows = (d.entries || []).map(function(e){
      var result = e.result || e.ok || "";
      return "<tr><td class=\"hint\">" + esc(e.ts || "") + "</td>"
        + "<td>" + esc(e.action || "") + "</td>"
        + '<td class="mono">' + esc(e.target || e.detail || "") + "</td>"
        + "<td>" + esc(result) + "</td></tr>";
    }).join("");
    tb.innerHTML = rows || '<tr><td colspan="4" class="hint">暂无审计记录</td></tr>';
    msg.textContent = "最近 " + ((d.entries || []).length) + " 条（密钥只记指纹）";
  } catch(e){
    tb.innerHTML = '<tr><td colspan="4" class="err">加载失败: ' + esc(e.message) + '</td></tr>';
  }
}

/* ---------------- 只读状态 ---------------- */
async function loadStatus(){
  try {
    var d = await api("status");
    var el = $("hotChip");
    if(el){
      var hot = d.hotPath || "unknown";
      el.textContent = "热路径 " + hot;
      el.className = "chip " + (hot === "available" ? "on" : (hot === "unavailable" ? "off" : ""));
      el.title = hot === "available" ? "reconciler --check 通过：可热应用"
        : (hot === "unavailable" ? "reconciler --check 不可用：apply 只能降级为 gen_config + docker restart"
                                 : "reconciler --check 结果未知");
    }
  } catch(e){ /* 状态徽标失败不阻塞页面 */ }
}

function bind(){
  $("cfInput").addEventListener("input", updateConfirmBtn);
  $("cfAck").addEventListener("change", updateConfirmBtn);
  $("cfCancel").addEventListener("click", function(){
    PENDING = null;
    $("confirmPanel").hidden = true;
  });
  $("cfConfirm").addEventListener("click", async function(){
    var p = PENDING;
    if(!p){ return; }
    var confirm = ($("cfInput").value || "").trim();
    if(confirm !== String(p.confirmExpected)){ toast("确认串不匹配，未执行", "err"); return; }
    if(p.ackRequired && !$("cfAck").checked){ toast("请先勾选「已知晓无法热应用」", "err"); return; }
    $("cfConfirm").disabled = true;
    var payload = {token: p.token, confirm: confirm};
    if(p.ackRequired){ payload.acknowledgeDegraded = true; }
    try {
      var data = await api(p.applyPath, payload);
      PENDING = null;
      $("confirmPanel").hidden = true;
      if(data && data.jobId){
        showJob(data.jobId, p.after);
      } else {
        handleInstant(data);
        if(p.after){ p.after(); }
      }
    } catch(e){
      toast("执行失败: " + e.message, "err");
      $("cfConfirm").disabled = false;
    }
  });
  $("copyKey").addEventListener("click", async function(){
    var v = $("oneKey").textContent || "";
    try { await navigator.clipboard.writeText(v); toast("已复制", "ok"); }
    catch(e){
      var r = document.createRange(); r.selectNodeContents($("oneKey"));
      var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
      toast("已选中，请按 Ctrl+C 复制", "ok");
    }
  });
  $("closeKey").addEventListener("click", function(){ $("oneKeyBox").hidden = true; $("oneKey").textContent = ""; });
  $("modelsRefresh").addEventListener("click", loadModels);
  $("modelsPreview").addEventListener("click", async function(){
    var sel = [];
    document.querySelectorAll("#modelsBox input:checked").forEach(function(x){ sel.push(x.value); });
    try {
      var pv = await api("models/preview", {models: sel});
      pv.applyPath = "models/apply";
      pv.after = loadModels;
      startConfirm(pv);
      $("modelsMsg").textContent = "已生成预览，请查看下方确认面板";
    } catch(e){ toast("预览失败: " + e.message, "err"); }
  });
  $("acctAdd").addEventListener("click", function(){
    var slot = parseInt($("newSlot").value, 10);
    var email = ($("newEmail").value || "").trim();
    var key = ($("newKey").value || "").trim();
    if(!slot){ toast("请填写槽号（1-999）", "err"); return; }
    var payload = {action: key ? "upsert" : "update", slot: slot};
    if(email){ payload.email = email; }
    if(key){ payload.key = key; }
    previewAccounts(payload);
  });
  $("acctTbl").addEventListener("click", function(ev){
    var b = ev.target.closest("button[data-act]");
    if(!b || BOOT.readonly){ return; }
    var act = b.dataset.act, slot = parseInt(b.dataset.slot, 10);
    if(act === "rotate"){
      var k = window.prompt("输入槽 a" + slot + " 的新上游 key（只写 .env，不回显）");
      if(!k || !k.trim()){ return; }
      previewAccounts({action: "rotate", slot: slot, key: k.trim()});
    } else if(act === "toggle"){
      previewAccounts({action: "toggle", slot: slot});
    } else if(act === "delete"){
      previewAccounts({action: "delete", slot: slot});
    }
  });
  $("keyIssue").addEventListener("click", async function(){
    var label = ($("newLabel").value || "").trim();
    if(!label){ toast("请填写 label", "err"); return; }
    try {
      var pv = await api("instance-keys/preview", {action: "issue", label: label});
      pv.applyPath = "instance-keys/apply";
      pv.after = loadKeys;
      startConfirm(pv);
    } catch(e){ toast("预览失败: " + e.message, "err"); }
  });
  $("keyTbl").addEventListener("click", async function(ev){
    var b = ev.target.closest("button[data-revoke]");
    if(!b || BOOT.readonly){ return; }
    var label = b.dataset.revoke;
    try {
      var pv = await api("instance-keys/preview", {action: "revoke", label: label});
      pv.applyPath = "instance-keys/apply";
      pv.after = loadKeys;
      startConfirm(pv);
    } catch(e){ toast("预览失败: " + e.message, "err"); }
  });
  $("affPreview").addEventListener("click", async function(){
    var el = document.querySelector('input[name="affmode"]:checked');
    if(!el){ toast("请选择模式", "err"); return; }
    try {
      var pv = await api("affinity/preview", {mode: el.value});
      pv.applyPath = "affinity/apply";
      pv.after = loadAffinity;
      startConfirm(pv);
    } catch(e){ toast("预览失败: " + e.message, "err"); }
  });
  $("auditRefresh").addEventListener("click", loadAudit);
}

/* 懒初始化：只在首次打开「管理控制台」页签时调用（由看板 showTab 触发）。 */
var INITED = false;
function adminInit(){
  if(INITED){ return; }
  INITED = true;
  var caps = BOOT.capabilities || {};
  var names = {models: "模型", accounts: "账号", keys: "实例键", affinity: "钉号", audit: "审计"};
  var chips = Object.keys(names).map(function(k){
    var on = !!caps[k];
    return '<span class="chip ' + (on ? "on" : "off") + '">' + esc(names[k]) + (on ? " ✓" : " ✗") + '</span>';
  }).join("");
  $("capChips").innerHTML = chips
    + '<span class="chip" id="hotChip">热路径 …</span>'
    + '<span class="chip">HOME: ' + esc(BOOT.home || "—") + '</span>'
    + '<span class="chip ' + (BOOT.reconcilerOk ? "on" : "off") + '">reconciler ' + (BOOT.reconcilerOk ? "就绪" : "缺失") + '</span>';
  if(BOOT.readonly){
    ["modelsPreview", "acctAdd", "keyIssue", "affPreview"].forEach(function(id){
      if($(id)){ $(id).disabled = true; }
    });
  }
  bind();
  loadModels(); loadAccounts(); loadKeys(); loadAffinity(); loadAudit(); loadStatus();
}
window.adminInit = adminInit;
})();
</script>
</div>
"""
