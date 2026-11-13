// 本地自检：只测纯函数（不需要 DSH 运行）。node selftest.mjs
import {
  applyMarker, resolveTreeKey, encodeTreeKey, decodeTreeKey, extractTreeKey, stripZeroWidth, __internals,
} from './index.js';

let pass = 0;
let fail = 0;
const ok = (name, cond, extra = '') => {
  if (cond) { pass += 1; console.log(`  PASS  ${name}`); }
  else { fail += 1; console.log(`  FAIL  ${name} ${extra}`); }
};

const msg = (content, source) => (source ? { role: 'user', content, source } : { role: 'user', content });
const MAGIC = __internals.ZW_MAGIC;
const countMagic = (text) => text.split(MAGIC).length - 1;
/** 「界面上可见/可读」的等价物：去掉零宽标记字符后剩下的文本。 */
const visible = (text) => stripZeroWidth(text);
const LEADER = 'session-4e7011c7-2246-4e22-8f91-3ca4f93e6295';

console.log('— 零宽编码往返 —');
{
  const enc = encodeTreeKey(LEADER);
  ok('uuid 形态：编码非空且全部为字母表字符', typeof enc === 'string'
    && [...enc].every((ch) => __internals.ZW_ALPHABET.includes(ch)), String(enc));
  ok('uuid 形态：载荷长度 = 4(MAGIC)+1(flag)+64+4(校验)+1(终止) = 74', enc.length === 74, String(enc.length));
  ok('uuid 形态：解回原键', decodeTreeKey(enc) === LEADER, String(decodeTreeKey(enc)));
  ok('uuid 形态：解码用尾部（混在文本里也能解）', decodeTreeKey('你好' + enc) === LEADER);
  const bare = '58822bbf-3aa3-463a-8933-91506896885d';
  ok('裸 uuid 往返', decodeTreeKey(encodeTreeKey(bare)) === bare);
  const other = 'unit-tree-9';
  ok('非 uuid 串往返', decodeTreeKey(encodeTreeKey(other)) === other);
  ok('非 uuid 串载荷自带长度（4+1+4+4*11+4+1=58）', encodeTreeKey(other).length === 58, String(encodeTreeKey(other).length));
  ok('尾部终止哨兵是 U+2060（非空白、trim 吃不掉）', enc.endsWith('\u2060'), JSON.stringify(enc.slice(-1)));
  ok('尾部终止哨兵也去不掉（trim 不变）', enc.trim().endsWith('\u2060'));
  ok('严格解码：载荷后面还有字符 → undefined', decodeTreeKey(enc + 'x') === undefined);
  ok('宽松解码：载荷后面还有 JSON 尾巴也能解（请求体场景）',
    extractTreeKey('{"content":"hi' + enc + '"}]}') === LEADER);
  ok('宽松解码：没有载荷 → undefined', extractTreeKey('{"content":"hi"}') === undefined);
  ok('非法键不编码（太短）', encodeTreeKey('ab') === undefined);
  ok('非法键不编码（含空格）', encodeTreeKey('bad key!') === undefined);
  ok('非法键不编码（超 64）', encodeTreeKey('a'.repeat(65)) === undefined);
  ok('坏载荷：MAGIC 后混入可见字符 → undefined', decodeTreeKey(MAGIC + 'abc') === undefined);
  ok('坏载荷：flag 保留位 → undefined', decodeTreeKey(enc.slice(0, 4) + '\u2060' + enc.slice(5)) === undefined);
  ok('无载荷：undefined', decodeTreeKey('普通文本') === undefined);
  // 回归：4 个零宽符号的 MAGIC 可能**碰巧出现在载荷内部**（约 0.4%/窗口）。
  // 若用 lastIndexOf(MAGIC) 定位就会解错/解不出 → 必须从左往右找第一个「解到末尾」的候选。
  let tricky = null;
  for (let i = 0; i < 5000 && tricky === null; i += 1) {
    const candidate = 'unit-tree-' + i;
    const e = encodeTreeKey(candidate);
    if (e.indexOf(MAGIC, 1) >= 0) tricky = { key: candidate, enc: e };
  }
  ok('构造出「载荷内部含 MAGIC」的样本', tricky !== null);
  if (tricky) {
    ok('内部含 MAGIC 仍能解回同一键', decodeTreeKey('正文' + tricky.enc) === tricky.key);
    ok('内部含 MAGIC 时仍幂等（不叠加）', applyMarker(
      applyMarker([msg('正文')], tricky.key), tricky.key).length === 1
      && (() => { const once = applyMarker([msg('正文')], tricky.key); return applyMarker(once, tricky.key) === once; })());
    ok('内部含 MAGIC 时可见文本仍是正文', visible(applyMarker([msg('正文')], tricky.key)[0].content) === '正文');
  }
  const corrupt = enc.slice(0, 10) + (enc[10] === '\u200b' ? '\u200c' : '\u200b') + enc.slice(11);
  ok('校验和：翻掉一个符号 → undefined', decodeTreeKey(corrupt) === undefined);
  ok('校验和：截断尾部 → undefined', decodeTreeKey(enc.slice(0, -4)) === undefined);
}
console.log('— 不可见：零宽载荷不含可读字符、不被 trim 掉 —');
{
  const enc = encodeTreeKey(LEADER);
  ok('载荷中可读文本长度为 0', visible(enc).length === 0, JSON.stringify(visible(enc)));
  ok('载荷不含 "pool-tree" 字样', !enc.includes('pool-tree'));
  ok('载荷不含任何 ASCII 可打印字符', !/[\x20-\x7e]/.test(enc));
  ok('载荷 trim() 后不变（不是空白字符）', enc.trim() === enc, JSON.stringify(enc.trim().length));
  ok('载荷末字符不可被 trim 掉', enc.slice(-1).trim() === enc.slice(-1));
  ok('载荷不含 U+FEFF（JS trim 会去掉它，故不用它做符号）', !enc.includes('\ufeff'));
}
console.log('— 标记注入（字符串 content）—');
{
  const messages = [msg('你好'), msg('帮我重构')];
  const out = applyMarker(messages, 'root-1');
  ok('追加到末条末尾', out !== messages && out[1].content === '帮我重构' + encodeTreeKey('root-1'), JSON.stringify(out[1]));
  ok('可见文本与原文逐字相同（界面上什么都没多）', visible(out[1].content) === '帮我重构', JSON.stringify(visible(out[1].content)));
  ok('可见文本不含 pool-tree', !visible(out[1].content).includes('pool-tree'));
  ok('全文也不含 pool-tree 字样', !out[1].content.includes('pool-tree'));
  ok('标记恰好出现一次（不叠加）', countMagic(out[1].content) === 1, String(countMagic(out[1].content)));
  ok('载荷在文本最末（末尾追加）', out[1].content.endsWith(encodeTreeKey('root-1')));
  ok('能解回树键', decodeTreeKey(out[1].content) === 'root-1');
  ok('首条不受影响', out[0] === messages[0]);
  ok('原数组未被改写', messages[1].content === '帮我重构');

  const again = applyMarker(out, 'root-1');
  ok('幂等：二次调用返回原引用', again === out);
}
console.log('— 过期标记：替换而非叠加 —');
{
  const stale = '旧标记' + encodeTreeKey('stale-9');
  const messages = [msg('a'), msg(stale)];
  const out = applyMarker(messages, 'root-2');
  ok('替换为当前树键', decodeTreeKey(out[1].content) === 'root-2');
  ok('可见文本回到原文', visible(out[1].content) === '旧标记', JSON.stringify(visible(out[1].content)));
  ok('不残留旧值', !out[1].content.includes(encodeTreeKey('stale-9')) && !visible(out[1].content).includes('stale-9'));
  ok('不叠加：仍是 1 个载荷', countMagic(out[1].content) === 1);
}
console.log('— 旧可见字面量升级为零宽（升级路径）—');
{
  const messages = [msg('老消息\n[pool-tree: root-9]')];
  const out = applyMarker(messages, 'root-9');
  ok('可见字面量被抹掉', !out[0].content.includes('[pool-tree:'), JSON.stringify(visible(out[0].content)));
  ok('可见文本只剩正文（换行保留）', visible(out[0].content) === '老消息\n', JSON.stringify(visible(out[0].content)));
  ok('同一树键继续可解', decodeTreeKey(out[0].content) === 'root-9');
  const messages2 = [msg('老消息\n[pool-tree: stale-9]')];
  const out2 = applyMarker(messages2, 'root-9');
  ok('旧字面量异值 → 换成新值', decodeTreeKey(out2[0].content) === 'root-9');
  ok('旧值不残留', !visible(out2[0].content).includes('stale-9'));
}
console.log('— parts 数组 —');
{
  const parts = [{ type: 'text', text: '看这个' }, { type: 'image', ref: 'x' }];
  const out = applyMarker([msg(parts)], 'root-3');
  ok('改写首个 text part（末尾追加）', out[0].content[0].text === '看这个' + encodeTreeKey('root-3'));
  ok('可见文本不变', visible(out[0].content[0].text) === '看这个');
  ok('非 text part 保持原样', out[0].content[1] === parts[1]);
}
{
  const parts = [{ type: 'image', ref: 'x' }];
  const out = applyMarker([msg(parts)], 'root-4');
  ok('无 text part：追加一个 text part', out[0].content.length === 2, JSON.stringify(out[0].content.map((p) => p.type)));
  ok('新 part 里只有零宽载荷（可读长度 0）', visible(out[0].content[1].text).length === 0);
  ok('新 part 仍可解回树键', decodeTreeKey(out[0].content[1].text) === 'root-4');
}
console.log('— runtime-context 消息必须跳过（DSH 会丢弃它，打上去等于没注入）—');
{
  const rc = msg('Current runtime context: cwd=…', { kind: 'runtime-context' });
  const messages = [msg('ping'), rc];
  const out = applyMarker(messages, 'root-5');
  ok('真消息拿到载荷', decodeTreeKey(out[0].content) === 'root-5', JSON.stringify(out[0]));
  ok('runtime-context 消息原样不动（MARKER 为 null 表示引用未变）', out[1] === rc);
  ok('runtime-context 上完全没有载荷', !out[1].content.includes(MAGIC));
  ok('可见文本合计仍是用户输入', visible(out[0].content) === 'ping');
}
{
  const only = [msg('ctx', { kind: 'runtime-context' })];
  const out = applyMarker(only, 'root-6');
  ok('只有 runtime-context：原样返回（fail-open，不硬塞）', out === only);
}
console.log('— 边界与失败开放 —');
{
  ok('空数组：原样返回', applyMarker([], 'root').length === 0);
  ok('非数组：原样返回', applyMarker(undefined, 'root') === undefined);
  ok('无 rootId：原样返回', applyMarker([msg('x')], undefined)[0].content === 'x');
  ok('非法 rootId：原样返回（不注入）', applyMarker([msg('x')], 'bad key')[0].content === 'x');
  ok('未知 content 类型：原样返回', applyMarker([{ role: 'user', content: 42 }], 'root')[0].content === 42);
  ok('未知 content 类型 + 前面有真消息：落到真消息上', decodeTreeKey(applyMarker(
    [{ role: 'user', content: 42 }, msg('ok')], 'root-7')[1].content) === 'root-7');
}
console.log('— 树键上溯 —');
{
  const headers = new Map([
    ['child', { id: 'child', parentSession: 'mid' }],
    ['mid', { id: 'mid', parentSession: 'root' }],
    ['root', { id: 'root' }],
  ]);
  ok('三级：上溯到根', resolveTreeKey(headers.get('child'), (id) => headers.get(id)) === 'root');
  ok('根自身：返回自己', resolveTreeKey(headers.get('root'), (id) => headers.get(id)) === 'root');
  const orphan = { id: 'orphan', parentSession: 'gone' };
  ok('父会话查不到：退化为已声明的父 id（不是自身）', resolveTreeKey(orphan, () => undefined) === 'gone');
  ok('缺 id：返回 undefined', resolveTreeKey({}, () => undefined) === undefined);
  const loop = new Map([['a', { id: 'a', parentSession: 'b' }], ['b', { id: 'b', parentSession: 'a' }]]);
  ok('环状父链：受 maxHops 限制不死循环', typeof resolveTreeKey(loop.get('a'), (id) => loop.get(id), 8) === 'string');
}
console.log('— 真实形态（取自 2026-10-12 的子会话头）—');
{
  // 实际取证：子会话 id 无前缀、parentSession 带 session- 前缀 → ctx.sessions.get 常查不到
  const realChild = {
    id: '58822bbf-3aa3-463a-8933-91506896885d',
    parentSession: 'session-4e7011c7-2246-4e22-8f91-3ca4f93e6295',
    origin: 'subagent',
    delegationDepth: 1,
  };
  const key = resolveTreeKey(realChild, () => undefined);
  ok('父查不到时仍等于父会话 id（= 队长会话）', key === LEADER, String(key));
  ok('绝不是子代理自身的 id', key !== realChild.id, String(key));
  const parentHeader = { id: LEADER, delegationDepth: 0 };
  ok('队长自身与子代理得到同一树键',
    resolveTreeKey(parentHeader, () => undefined) === resolveTreeKey(realChild, () => undefined));
  ok('该真实树键可编码/解码往返', decodeTreeKey(encodeTreeKey(key)) === key);
}
console.log('— 出站请求体形态（模型看得到，界面看不到）—');
{
  const key = LEADER;
  const out = applyMarker([msg('ping')], key);
  const body = JSON.stringify({
    model: 'deepseek-v4.1-flash',
    messages: [{ role: 'user', content: out[0].content }],
  });
  ok('请求体原始字节里含零宽载荷（raw UTF-8 形态）', body.includes(encodeTreeKey(key)));
  ok('请求体里没有可读的 pool-tree 字样', !body.includes('pool-tree'));
  ok('从请求体字节里取出载荷即可解回树键',
    decodeTreeKey(body.slice(body.indexOf(MAGIC), body.indexOf(MAGIC) + encodeTreeKey(key).length)) === key);
}
console.log('— 可配置标记名（仅用于识别/升级旧可见字面量）—');
{
  const out = applyMarker([msg('x [dsh-tree: abc12345]')], 'root-5', 'dsh-tree');
  ok('自定义 markerKey 的旧字面量被升级为零宽', decodeTreeKey(out[0].content) === 'root-5'
    && !out[0].content.includes('[dsh-tree:'), JSON.stringify(visible(out[0].content)));
  ok('正则随 markerKey 变化', __internals.markerRegex('dsh-tree').test('y [dsh-tree: abc12345]'));
  ok('默认 markerKey 不认别的名字', !__internals.markerRegex('pool-tree').test('y [dsh-tree: abc12345]'));
}

console.log(`\nselftest: pass=${pass} fail=${fail}`);
process.exit(fail ? 1 : 0);
