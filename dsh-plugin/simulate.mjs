// 插件集成测试：模拟 agent/pre-step 回调链，用「真实取证的会话头」验证注入结果。
// node simulate.mjs
import { apply, decodeTreeKey, extractTreeKey, stripZeroWidth, __internals } from './index.js';

let pass = 0, fail = 0;
const ok = (name, cond, extra = '') => {
  if (cond) { pass += 1; console.log(`  PASS  ${name}`); }
  else { fail += 1; console.log(`  FAIL  ${name} ${extra}`); }
};

// ---- 真实数据（2026-10-12 从 ~/.dsh/sessions 解压取得）----
const CWD = 'C:\\Users\\<user>\\Desktop\\project';   // 已脱敏：测试不依赖真实用户名/路径
const LEADER = {
  id: 'session-4e7011c7-2246-4e22-8f91-3ca4f93e6295',
  cwd: CWD,
  isSeeded: false, delegationDepth: 0, agentPreset: 'standard',
};
const CHILD = {
  id: '58822bbf-3aa3-463a-8933-91506896885d',
  cwd: CWD,
  parentSession: 'session-4e7011c7-2246-4e22-8f91-3ca4f93e6295',
  origin: 'subagent', delegationDepth: 1, agentPreset: 'cordis',
};

function makeHarness(sessionLookup = () => undefined) {
  const listeners = [];
  const ctx = {
    on(name, fn) { listeners.push({ name, fn }); },
    sessions: { get: sessionLookup },
    logger: { debug() {} },
  };
  apply(ctx, { enabled: true, markerKey: 'pool-tree', maxHops: 8 });
  return listeners;
}

const enterDecision = (messages) => async () => ({ kind: 'enter', messages, startsRequestSeries: true });
const agentOf = (header) => ({ id: header.id, session: { header } });

/** 会话里那条「真实存在、会被 DSH 持久化并渲染」的消息 = decision.messages 里的非 runtime-context 消息。 */
const committed = (decision) => decision.messages.filter((m) => m?.source?.kind !== 'runtime-context');
/** 「用户在界面上能看到的文本」：所有被落库消息的内容去掉零宽标记字符。 */
const visibleText = (decision) => committed(decision)
  .map((m) => (typeof m.content === 'string' ? m.content : JSON.stringify(m.content)))
  .map(stripZeroWidth).join('\n');

const MSGS = [{ role: 'user', content: 'ping' }];
const MAGIC = __internals.ZW_MAGIC;

console.log('— 回调注册 —');
const L = makeHarness(() => undefined);
ok('恰好注册一个监听器', L.length === 1, String(L.length));
ok('挂在 agent/pre-step 上', L[0]?.name === 'agent/pre-step', L[0]?.name);

console.log('— 子代理（父会话查不到的真实情形）—');
{
  const d = await L[0].fn({ agent: agentOf(CHILD) }, enterDecision(MSGS));
  ok('decision 类型保留（startsRequestSeries 未丢）', d.kind === 'enter' && d.startsRequestSeries === true);
  ok('树键 = 队长会话 id（从载荷解出）', decodeTreeKey(d.messages[0].content) === LEADER.id, JSON.stringify(decodeTreeKey(d.messages[0].content)));
  ok('不是子代理自身 id', decodeTreeKey(d.messages[0].content) !== CHILD.id);
  ok('界面上看到的仍是原文（零可读增量）', visibleText(d) === 'ping', JSON.stringify(visibleText(d)));
  ok('落库文本里没有 pool-tree 字样', !committed(d)[0].content.includes('pool-tree'));
  ok('载荷恰好一个（不叠加）', committed(d)[0].content.split(MAGIC).length - 1 === 1);
  ok('载荷在消息文本最末（末尾追加）', committed(d)[0].content.slice(-1) !== 'g' && committed(d)[0].content.startsWith('ping'));
}

console.log('— 队长自身 —');
{
  const d = await L[0].fn({ agent: agentOf(LEADER) }, enterDecision([{ role: 'user', content: 'ping' }]));
  ok('树键 = 自身会话 id', decodeTreeKey(d.messages[0].content) === LEADER.id, JSON.stringify(decodeTreeKey(d.messages[0].content)));
  ok('界面文本不变', visibleText(d) === 'ping');
}

console.log('— 同一棵树必须得到同一标记 —');
{
  const a = await L[0].fn({ agent: agentOf(CHILD) }, enterDecision([{ role: 'user', content: 'x' }]));
  const b = await L[0].fn({ agent: agentOf(LEADER) }, enterDecision([{ role: 'user', content: 'y' }]));
  ok('队长与子代理树键相同', decodeTreeKey(a.messages[0].content) === decodeTreeKey(b.messages[0].content),
    `${decodeTreeKey(a.messages[0].content)} vs ${decodeTreeKey(b.messages[0].content)}`);
}

console.log('— runtime-context 消息必须被跳过 —');
{
  const rc = { role: 'user', content: 'Current runtime context: cwd=…', source: { kind: 'runtime-context' } };
  const d = await L[0].fn({ agent: agentOf(CHILD) }, enterDecision([{ role: 'user', content: 'ping' }, rc]));
  ok('载荷落在真正的用户消息上', decodeTreeKey(d.messages[0].content) === LEADER.id);
  ok('runtime-context 消息原样未动（DSH 会用自己的投影顶替它）', d.messages[1] === rc);
  ok('runtime-context 上没有载荷', !rc.content.includes(MAGIC));
  ok('界面文本仍只有用户输入', visibleText(d) === 'ping');
}
{
  const only = [{ role: 'user', content: 'ctx', source: { kind: 'runtime-context' } }];
  const d = await L[0].fn({ agent: agentOf(CHILD) }, enterDecision(only));
  ok('只有 runtime-context：不注入（fail-open 原样返回）', d.messages === only);
}

console.log('— 幂等（同一步重复进入）—');
{
  const d1 = await L[0].fn({ agent: agentOf(CHILD) }, enterDecision([{ role: 'user', content: 'ping' }]));
  const d2 = await L[0].fn({ agent: agentOf(CHILD) }, enterDecision(d1.messages));
  ok('二次进入：返回同一条消息引用（未再追加）', d2.messages === d1.messages || d2.messages[0] === d1.messages[0]);
  ok('载荷仍只有一个', d2.messages[0].content.split(MAGIC).length - 1 === 1);
}

console.log('— 多级委派（能查到中间会话时继续上溯）—');
{
  const mid = { id: 'mid-uuid', parentSession: LEADER.id, origin: 'subagent', delegationDepth: 1 };
  const grand = {
    id: 'grand-uuid', parentSession: 'session-mid-uuid', origin: 'subagent', delegationDepth: 2,
  };
  // 注意：parentOf 期望的是「Session 对象」（有 .header），不是裸 header —— 这正是集成测试的价值
  const lookup = (id) => {
    if (id === 'session-mid-uuid' || id === mid.id) return { header: mid };
    if (id === LEADER.id) return { header: LEADER };
    return undefined;
  };
  const L2 = makeHarness(lookup);
  const d = await L2[0].fn({ agent: agentOf(grand) }, enterDecision([{ role: 'user', content: 'ping' }]));
  ok('三级：上溯到队长会话', decodeTreeKey(d.messages[0].content) === LEADER.id, JSON.stringify(decodeTreeKey(d.messages[0].content)));
}

console.log('— 出站请求体：模型看得到 / 界面看不到 —');
{
  const d = await L[0].fn({ agent: agentOf(CHILD) }, enterDecision([{ role: 'user', content: 'ping' }]));
  const body = JSON.stringify({ model: 'deepseek-v4.1-flash', messages: d.messages });
  ok('请求体字节里含零宽载荷', body.includes(MAGIC));
  ok('请求体字节里没有可读 pool-tree', !body.includes('pool-tree'));
  ok('从请求体字节能解回树键（宽松解码：后面还有 JSON 尾巴）', extractTreeKey(body) === LEADER.id, String(extractTreeKey(body)));
  ok('严格解码（要求载荷落在文本末尾）在这里必须失败——证明「末尾追加」是可判定的', decodeTreeKey(body) === undefined);
  ok('请求体里人类可读文本末尾仍是 "ping"', stripZeroWidth(body).includes('"content":"ping"'), stripZeroWidth(body).slice(0, 160));
}

console.log('— 边界与失败开放 —');
{
  const reject = await L[0].fn({ agent: agentOf(CHILD) }, async () => ({ kind: 'reject' }));
  ok('下游 reject：原样返回', reject.kind === 'reject' && reject.messages === undefined);
  const d = await L[0].fn({ agent: agentOf(CHILD) }, enterDecision([]));
  ok('空消息数组：原样返回', Array.isArray(d.messages) && d.messages.length === 0);
  const noSession = await L[0].fn({ agent: { id: 'x' } }, enterDecision([{ role: 'user', content: 'ping' }]));
  ok('无 session.header：不注入（不抛错）', noSession.messages[0].content === 'ping');
  const weird = await L[0].fn({}, enterDecision([{ role: 'user', content: 'ping' }]));
  ok('空 payload：不抛错', weird.kind === 'enter');
  let threw = false;
  try { await L[0].fn({ agent: agentOf(CHILD) }, async () => { throw new Error('downstream'); }); }
  catch { threw = true; }
  ok('下游异常：向上传播（不吞框架错误）', threw);
}

console.log(`\nsimulate: pass=${pass} fail=${fail}`);
process.exit(fail ? 1 : 0);
