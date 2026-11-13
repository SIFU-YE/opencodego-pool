/**
 * @local/dsh-pool-affinity —— 号池会话钉号（树键注入 · 零宽编码 v2）
 *
 * 设计目标
 *   让「一个会话里启动的团队」的全部模型开销，都落在号池为这个会话选定的那一个账号上。
 *
 * 本插件只做一件事
 *   在进入模型的消息序列末尾附加一个稳定的「会话树键」标记，号池 sidecar 读到后
 *   按会话树选号并注入 x-litellm-tags，把整棵树钉到同一账号。
 *
 * v1.0.0/v1.0.1 的做法与缺陷（本次修复的 bug）
 *   旧实现把**可见字面量** `[pool-tree: <rootSessionId>]` 追加到最后一条消息文本末尾。
 *   `agent/pre-step` 的 decision.messages 会被 DSH **原样持久化**：
 *     dsh-agent-loop/lib/index.js  L1067-L1073
 *       for (const message of decision.messages)
 *         this.session.append("user/message", message, { surfaceOp: "append" })
 *   而 GUI 转录渲染的正是这些 `user/message` 事件 → 用户肉眼看到 `[pool-tree: …]`。
 *
 * v2.0.0 的做法：零宽编码（invisible，但仍在同一位置、仍是同一接缝）
 *   把树键编成 **零宽（default-ignorable）字符** 序列追加在消息文本**末尾**：
 *     字母表（4 个安全零宽字符，均非 Unicode White_Space、JS trim() 不会去掉）：
 *       0 = U+200B ZERO WIDTH SPACE
 *       1 = U+200C ZERO WIDTH NON-JOINER
 *       2 = U+200D ZERO WIDTH JOINER
 *       3 = U+2060 WORD JOINER
 *     载荷 = MAGIC(U+2060 U+200D U+200B U+200C) + 1 位 flag + body + 4 符号校验和 + 1 符号终止哨兵
 *       flag 0 = 裸 uuid（32 hex，去横线）→ 16 字节 → 64 符号
 *       flag 1 = session- 前缀 uuid           → 16 字节 → 64 符号
 *       flag 2 = 其它 [A-Za-z0-9_-]{4,64} 串  → 长度 4 符号 + 每字节 4 符号
 *       校验和 = sum(flag+body 各位) mod 256（挡掉「载荷内部碰巧出现 MAGIC」与截断）
 *       终止哨兵 = U+2060 WORD JOINER（非空白、不可断行 → 消息末字符不会被任何 trim 吃掉）
 *   常见树键（session-<uuid>）共 4+1+64+4+1 = 74 个零宽字符：不可见、不可复制阅读、
 *   不占可见宽度，但**原样存在于出站请求体字节里**，sidecar 直接按字节解码。
 *
 * 为什么不做「模型可见但界面不渲染」的隐藏通道（调研结论，见 README「接缝调研」）
 *   · `agent/pre-step` 的 decision.messages 只会以 `surfaceOp: 'append'` 落库
 *     （dsh-agent-loop/lib/index.js L1067-L1073），落库即渲染，没有隐藏 part 类型。
 *   · 唯一「只给模型看」的形态是 surface **replacement**（dsh-client-ui-chat
 *     L9464-L9471：replacement copies stay model-only），但：
 *       - 它只能由事件 append 产生，而 pre-step 无权指定 surfaceOp；
 *       - 想在「用户消息落库之后、buildRequest 之前」补一条 replace 事件，
 *         必须从 `session/event` 同步回调里再 append，而这被 dsh-session
 *         L1568-L1572 明确拒绝（"session append cannot reenter …"）；
 *       - 用 message projection（ctx.sessions.registerMessageProjection）虽然能
 *         「改派生内容不改落库内容」，但要预测未来 seq 或让首个请求丢标记，且
 *         插件被卸载后使用过该 projection 的会话会拒绝再派生（L1724-L1726）。
 *     结论：接缝代价与回归风险都高于零宽编码，故不采用。
 *
 * 三条不变量
 *   1. 标记值在同一会话树内恒定 → 出站前缀不变 → 不伤前缀缓存（只在消息尾部追加，前缀更稳）
 *   2. 幂等 → 同一消息不会被反复追加（已有载荷则改写值，不叠加）
 *   3. 全失败开放（fail-open）→ 任何异常/无法编码/无可注入目标都原样返回下游决策
 *
 * 只读：不写会话事件、不新增事件类型、不改工具、不改提示词装配。
 */

const DEFAULTS = {
  enabled: true,
  markerKey: 'pool-tree',
  maxHops: 8,
};

/* ============================================================ 零宽编码 v2 */

/** 4 个安全零宽字符：都不是 Unicode White_Space，JS trim()/Python strip() 都去不掉。 */
export const ZW_ALPHABET = ['\u200b', '\u200c', '\u200d', '\u2060'];
const ZW_INDEX = new Map(ZW_ALPHABET.map((ch, i) => [ch, i]));
/** 载荷起始哨兵：WJ ZWJ ZWSP ZWNJ —— 4 个字符互不相同，几乎不可能自然出现。 */
export const ZW_MAGIC = '\u2060\u200d\u200b\u200c';
/**
 * 载荷**尾部**终止哨兵：U+2060 WORD JOINER。
 * 它不可见、不可断行、不是 Unicode White_Space（JS trim()/Python strip() 都去不掉），
 * 用来保证「消息最末一个字符永远是一个不可被任何空白清理吃掉的零宽字符」。
 * （字母表里刻意不含 U+FEFF——它在 JS 的 trim() 白名单里，会被末尾清理吃掉。）
 */
export const ZW_TERMINATOR = '\u2060';
/** 与 sidecar 的 `_TREE_RE` 同形：树键必须是 4..64 个 [A-Za-z0-9_-]。 */
const KEY_SHAPE = /^[A-Za-z0-9_-]{4,64}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SESSION_UUID_RE = /^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function symbolOf(value) {
  return ZW_ALPHABET[value & 3];
}

/**
 * 把树键编码成零宽字符载荷；键不合法（空/超长/非 [A-Za-z0-9_-]）时返回 undefined。
 * 纯函数，无状态。
 *
 * 载荷 = MAGIC + flag + body + 校验和(4 符号)
 *   · body 自描述长度（uuid 形态定长 64，raw 形态带 1 字节长度），解码端「读完即止」，
 *     不依赖载荷后面是什么（请求体里后面还有 JSON 尾巴）；
 *   · 末尾 4 符号 = sum(flag + body 各位) mod 256，用于挡掉「正文里碰巧出现 MAGIC」
 *     与任何截断/损坏（零宽字符只有 4 个，4 符号窗口约 0.4% 概率撞上 MAGIC）。
 */
export function encodeTreeKey(key) {
  if (typeof key !== 'string' || !KEY_SHAPE.test(key)) return undefined;
  let flag;
  let body;
  if (SESSION_UUID_RE.test(key)) {
    flag = 1;
    body = key.slice('session-'.length).replace(/-/g, '');
  } else if (UUID_RE.test(key)) {
    flag = 0;
    body = key.replace(/-/g, '');
  } else {
    flag = 2;
    body = key;
  }
  const digits = [flag];
  if (flag === 2) {
    const bytes = new TextEncoder().encode(body);
    digits.push(...digitByte(bytes.length));
    for (const byte of bytes) digits.push(...digitByte(byte));
  } else {
    for (let i = 0; i < body.length; i += 2) digits.push(...digitByte(parseInt(body.slice(i, i + 2), 16)));
  }
  const out = ZW_MAGIC + digits.map((d) => symbolOf(d)).join('');
  return out + digitByte(checksum(digits)).map((d) => symbolOf(d)).join('') + ZW_TERMINATOR;
}

/** 一个字节 → 4 个符号值（MSB 在前）。 */
function digitByte(byte) {
  return [(byte >> 6) & 3, (byte >> 4) & 3, (byte >> 2) & 3, byte & 3];
}

/** 载荷校验和：flag + body 各位之和 mod 256。 */
function checksum(digits) {
  let sum = 0;
  for (const d of digits) sum += d;
  return sum & 0xff;
}

/** 从 from 起读 count 个字母表符号；不足或遇到非字母表字符返回 undefined。 */
function readSymbols(text, from, count) {
  const digits = [];
  let i = from;
  while (digits.length < count) {
    if (i >= text.length) return undefined;
    const digit = ZW_INDEX.get(text[i]);
    if (digit === undefined) return undefined;
    digits.push(digit);
    i += 1;
  }
  return { digits, end: i };
}

function combDigit(digits) {
  return (digits[0] << 6) | (digits[1] << 4) | (digits[2] << 2) | digits[3];
}

/** 在给定的 MAGIC 位置解码一个载荷；返回 { key, end } 或 undefined。 */
function decodeAt(text, at) {
  let cursor = at + ZW_MAGIC.length;
  const head = readSymbols(text, cursor, 1);
  if (head === undefined) return undefined;
  const flag = head.digits[0];
  cursor = head.end;
  const digits = [flag];
  let key;
  if (flag === 0 || flag === 1) {
    const body = readSymbols(text, cursor, 64);
    if (body === undefined) return undefined;
    cursor = body.end;
    digits.push(...body.digits);
    let hex = '';
    for (let i = 0; i < body.digits.length; i += 4) {
      hex += combDigit(body.digits.slice(i, i + 4)).toString(16).padStart(2, '0');
    }
    const uuid = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    key = flag === 1 ? `session-${uuid}` : uuid;
  } else if (flag === 2) {
    const length = readSymbols(text, cursor, 4);
    if (length === undefined) return undefined;
    const size = combDigit(length.digits);
    if (size < 4 || size > 64) return undefined;
    cursor = length.end;
    const body = readSymbols(text, cursor, size * 4);
    if (body === undefined) return undefined;
    cursor = body.end;
    digits.push(...length.digits, ...body.digits);
    const bytes = [];
    for (let i = 0; i < body.digits.length; i += 4) bytes.push(combDigit(body.digits.slice(i, i + 4)));
    try {
      key = new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(bytes));
    } catch {
      return undefined;
    }
  } else {
    return undefined;   // flag 3 保留
  }
  if (!KEY_SHAPE.test(key)) return undefined;
  const chk = readSymbols(text, cursor, 4);
  if (chk === undefined || combDigit(chk.digits) !== checksum(digits)) return undefined;
  cursor = chk.end;
  if (text[cursor] === ZW_TERMINATOR) cursor += 1;   // 尾部终止哨兵（可选）
  return { key, start: at, end: cursor };
}

/** 依次枚举文本里每个 MAGIC 位置，返回第一个「能解出合法载荷」的结果。 */
function scan(text, accept) {
  if (typeof text !== 'string') return undefined;
  let from = 0;
  for (;;) {
    const at = text.indexOf(ZW_MAGIC, from);
    if (at < 0) return undefined;
    const decoded = decodeAt(text, at);
    if (decoded !== undefined && accept(decoded)) return decoded;
    from = at + 1;
  }
}

/** 严格匹配：载荷必须正好落在文本末尾；返回 { key, start, end } 或 undefined。 */
function matchTreeKey(text) {
  return scan(text, (d) => d.end === text.length);
}

/**
 * 严格解码：**载荷必须正好落在文本末尾**（消息文本场景）。
 * 从最左的 MAGIC 开始试——真正的载荷起点一定在「正文里碰巧出现的 MAGIC」之前，
 * 所以第一个「解到文本末尾」的候选就是它；这样即使载荷内部碰巧出现 MAGIC 也不会误判。
 */
export function decodeTreeKey(text) {
  const hit = matchTreeKey(text);
  return hit === undefined ? undefined : hit.key;
}

/**
 * 宽松解码：允许载荷后面还有别的字节（raw 请求体场景；sidecar 的等价实现）。
 * 同样取最左的有效载荷（一个会话树内所有载荷值相同，取最左等价取最新以外的最稳）。
 */
export function extractTreeKey(text) {
  const hit = scan(text, () => true);
  return hit === undefined ? undefined : hit.key;
}

/** 丢掉零宽载荷字符后的「人类可读文本」——供自检断言「界面上不可见」用。 */
export function stripZeroWidth(text) {
  if (typeof text !== 'string') return text;
  let out = '';
  for (const ch of text) if (!ZW_INDEX.has(ch)) out += ch;
  return out;
}

/* ============================================================ 标记读写 */

/** 旧版可见字面量的正则：只用于把历史遗留的可见标记升级成零宽载荷。 */
function markerRegex(key) {
  const escaped = String(key).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp('\\[' + escaped + ':[ \\t]*([A-Za-z0-9_-]{4,64})\\]');
}

/** 取消息文本（content 可能是 string 或 parts 数组）。 */
function messageText(message) {
  const c = message && message.content;
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) {
    for (const part of c) {
      if (part && typeof part.text === 'string') return part.text;
    }
  }
  return undefined;
}

/**
 * 该消息是否是可注入目标。
 * 关键：跳过 `source.kind === 'runtime-context'` 的消息 ——
 * dsh-agent-loop/lib/index.js L1067-L1073 会用**重新投影的 context** 顶替它们，
 * 我们改的那一份会被丢弃（等于没注入），所以必须落到别的消息上。
 */
function isEligible(message) {
  if (!message || typeof message !== 'object') return false;
  const src = message.source;
  if (src && typeof src === 'object' && src.kind === 'runtime-context') return false;
  return true;
}

/** 读某条消息里已存在的标记值（零宽优先，其次旧可见字面量）。 */
function readMarker(message, legacyRx) {
  const text = messageText(message);
  if (typeof text !== 'string') return undefined;
  const zw = decodeTreeKey(text);
  if (zw !== undefined) return zw;
  const m = legacyRx.exec(text);
  return m ? m[1] : undefined;
}

/**
 * 把载荷写到文本末尾：已有零宽载荷则替换它，已有旧可见字面量则把它擦掉（升级），
 * 否则直接追加。绝不改动前缀可见文本。
 */
function stampText(text, encoded, legacyRx) {
  let base = text;
  const hit = matchTreeKey(base);
  if (hit !== undefined) base = base.slice(0, hit.start);   // 只删载荷本身，绝不动正文
  if (legacyRx.test(base)) base = base.replace(legacyRx, '');
  return base + encoded;
}

/**
 * 返回注入了标记的新消息（不改原对象）；无法处理时返回 undefined 表示“保持不变”。
 */
function stampMessage(message, encoded, legacyRx) {
  if (!message || typeof message !== 'object') return undefined;
  const c = message.content;
  if (typeof c === 'string') {
    const next = stampText(c, encoded, legacyRx);
    return next === c ? undefined : { ...message, content: next };
  }
  if (Array.isArray(c)) {
    const parts = c.slice();
    for (let i = 0; i < parts.length; i += 1) {
      const part = parts[i];
      if (part && typeof part.text === 'string') {
        const next = stampText(part.text, encoded, legacyRx);
        if (next === part.text) return undefined;
        parts[i] = { ...part, text: next };
        return { ...message, content: parts };
      }
    }
    parts.push({ type: 'text', text: encoded });
    return { ...message, content: parts };
  }
  return undefined;
}

/**
 * 给整段消息序列盖上树键标记（幂等、只在末尾追加、fail-open）。
 * 返回新数组，或原数组（表示无需改动）。
 */
export function applyMarker(messages, rootId, markerKey = DEFAULTS.markerKey) {
  if (!Array.isArray(messages) || messages.length === 0) return messages;
  if (typeof rootId !== 'string' || rootId.length === 0) return messages;
  const encoded = encodeTreeKey(rootId);
  if (encoded === undefined) return messages;      // 键不合 sidecar 形状 → 不注入
  const legacyRx = markerRegex(markerKey);

  // 1) 已有标记：同值短路（幂等）；异值/旧字面量 → 原位改写成零宽载荷。
  for (let i = 0; i < messages.length; i += 1) {
    if (!isEligible(messages[i])) continue;
    const text = messageText(messages[i]);
    if (typeof text !== 'string') continue;
    const zw = decodeTreeKey(text);
    const legacy = legacyRx.exec(text);
    if (zw === undefined && legacy === null) continue;
    if (zw === rootId) return messages;              // 零宽载荷已就位 → 幂等短路
    const stamped = stampMessage(messages[i], encoded, legacyRx);
    if (!stamped) continue;
    const out = messages.slice();
    out[i] = stamped;
    return out;
  }

  // 2) 都没有标记：追加到最后一条「可注入」消息（跳过 runtime-context）。
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (!isEligible(messages[i])) continue;
    const stamped = stampMessage(messages[i], encoded, legacyRx);
    if (!stamped) continue;
    const out = messages.slice();
    out[i] = stamped;
    return out;
  }
  return messages;                                  // 无可注入目标 → 原样返回（fail-open）
}

/**
 * 解析会话树的根会话 id。
 * parentOf(sessionId) 返回该会话的 SessionHeader（找不到返回 undefined）。
 *
 * 关键细节（2026-10-12 由真实子会话头取证）：
 *   SessionHeader.id 形如 "58822bbf-…"（无前缀），而 parentSession 形如
 *   "session-4e7011c7-…"（带 session- 前缀）——形态不一致，因此
 *   ctx.sessions.get(parentId) 常常查不到。此时**必须退回 parentSession 的值本身**，
 *   绝不能退回子会话自己的 id（否则每个子代理各自成树，钉号失效）。
 */
export function resolveTreeKey(header, parentOf, maxHops = DEFAULTS.maxHops) {
  if (!header || typeof header.id !== 'string' || header.id.length === 0) return undefined;
  let current = header;
  let key = header.id;
  for (let hop = 0; hop < maxHops; hop += 1) {
    const parentId = current.parentSession;
    if (typeof parentId !== 'string' || parentId.length === 0) break;
    key = parentId;                                   // 已声明的父 id 先作为树键
    const parent = parentOf(parentId);
    if (!parent || typeof parent.id !== 'string') break;  // 查不到就停在这一层（键仍是父 id）
    current = parent;
    key = parent.id;
  }
  return key;
}

export function apply(ctx, rawConfig) {
  const config = { ...DEFAULTS, ...(rawConfig && typeof rawConfig === 'object' ? rawConfig : {}) };
  if (config.enabled === false) return;

  const markerKey = typeof config.markerKey === 'string' && config.markerKey.length > 0
    ? config.markerKey
    : DEFAULTS.markerKey;
  const maxHops = Number.isInteger(config.maxHops) && config.maxHops > 0 ? config.maxHops : DEFAULTS.maxHops;

  const cache = new Map();

  const parentOf = (sessionId) => {
    try {
      const direct = ctx.sessions?.get?.(sessionId);
      if (direct?.header) return direct.header;
      // id 形态不一致（父 id 常带 session- 前缀而 store 可能按裸 id 索引）→ 再试一次去前缀
      if (typeof sessionId === 'string' && sessionId.startsWith('session-')) {
        const stripped = ctx.sessions?.get?.(sessionId.slice('session-'.length));
        if (stripped?.header) return stripped.header;
      }
      return undefined;
    } catch {
      return undefined;
    }
  };

  const treeKeyOf = (agent) => {
    try {
      const header = agent?.session?.header;
      if (!header || typeof header.id !== 'string') return undefined;
      const cached = cache.get(header.id);
      if (cached !== undefined) return cached;
      const key = resolveTreeKey(header, parentOf, maxHops) ?? header.id;
      cache.set(header.id, key);
      return key;
    } catch {
      return undefined;
    }
  };

  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next();
    try {
      if (!decision || decision.kind !== 'enter') return decision;
      const rootId = treeKeyOf(payload?.agent);
      if (!rootId) return decision;
      const messages = applyMarker(decision.messages, rootId, markerKey);
      if (messages === decision.messages) return decision;
      // 官方约束：改写时要 spread，保住 startsRequestSeries 等字段
      return { ...decision, messages };
    } catch {
      return decision; // fail-open
    }
  });

  ctx.logger?.debug?.('[dsh-pool-affinity] active zw-marker=%s maxHops=%d', markerKey, maxHops);
}

/** 供本地自检使用（非插件契约）。 */
export const __internals = {
  markerRegex, stampMessage, stampText, readMarker, isEligible, messageText,
  ZW_MAGIC, ZW_ALPHABET, KEY_SHAPE,
};