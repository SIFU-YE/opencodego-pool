#!/usr/bin/env node
/**
 * cpt-mcp · 公开推送前的敏感信息门禁（零依赖，不安装任何工具）。
 *
 * 目的：在 `git push` 前拦住“绝不应该出现在公开仓库里的内容”。
 * 这不是通用防病毒扫描，也不是 Git 历史清理工具；它只回答一个问题：
 * 当前工作树里有没有凭据、真实 PII、内网地址、本机账号路径或内部业务标识？
 *
 * 检查范围：
 *   1. 明文口令 / API token / 私钥 / 云厂商密钥；
 *   2. 真实手机号、身份证号、非示例邮箱；
 *   3. 私有 IP、内网域名、本机 Windows 用户目录 / Unix home 路径；
 *   4. 内部模板名、业务表名、字段名——**只存 SHA-256，不把明文写进本文件**。
 *
 * 用法：
 *   node scripts/scan-secrets.mjs                 # 扫当前仓库（跳过 docs/demo 等内部目录）
 *   node scripts/scan-secrets.mjs --root <dir>    # 扫指定目录（例如公开快照 worktree）
 *   node scripts/scan-secrets.mjs --include-docs  # 连 docs 一起扫（内部仓库自查用）
 *
 * 退出码：0 = 通过；1 = 命中。命中内容只报告文件/行/规则，不回显敏感值。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const argv = process.argv.slice(2);
const argValue = (name, fallback) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const ROOT = resolve(argValue('--root', join(dirname(fileURLToPath(import.meta.url)), '..')));
const INCLUDE_DOCS = argv.includes('--include-docs');

/** 这些目录不是公开发布内容；默认跳过（公开快照本来就不含 docs/demo）。 */
const SKIP_DIRS = new Set([
  '.git', 'node_modules', '.cpt-mcp', '.fr-preview', '.fr-engine-check',
  '.trash', '.work', 'dist', 'tmp_test', 'demo',
  ...(INCLUDE_DOCS ? [] : ['docs']),
]);
const SKIP_EXT = new Set(['.tgz', '.png', '.jpg', '.jpeg', '.ico', '.zip', '.pdf', '.sqlite', '.db']);
const MAX_BYTES = 2 * 1024 * 1024;

const RULES = [
  // 明文口令赋值：只匹配“整行字面量赋值”，不匹配 password: cfg.password / process.env.* 这类代码。
  [/^\s*(?:MYSQL_PASSWORD|MYSQL_PASSWD|DB_PASSWORD|DB_PASSWD|PASSWORD|PASSWD|SECRET|API_KEY|ACCESS_TOKEN)\s*[:=]\s*['"][^'"\n]{4,}['"]\s*[,;]?\s*$/im, '明文的凭据赋值'],
  [/^\s*(?:MYSQL_PASSWORD|MYSQL_PASSWD|DB_PASSWORD|DB_PASSWD|PASSWORD|PASSWD|SECRET|API_KEY|ACCESS_TOKEN)\s*=\s*(?!['"]|process\.env|\$\{|<|\[)(?!.*(?:your_|example|placeholder|xxx|changeme|dummy|raw\.|conn\.|cfg\.|opts\.))([^\s#'"]{4,})\s*$/im, '明文的凭据赋值'],
  // 私钥
  [/-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----/, '私钥'],
  // GitHub / Gitee / 云厂商 token
  [/\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/, 'GitHub 令牌'],
  [/\bAKIA[0-9A-Z]{16}\b/, 'AWS Access Key'],
  [/\bxox[baprs]-[A-Za-z0-9-]{10,}\b/, 'Slack 令牌'],
  // 私有 IP（127.0.0.1 / 0.0.0.0 与本机测试地址除外）
  [/\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b/, '私有 IP 地址'],
  // 内网域名（保留示例域名除外）
  [/\b(?:[a-z0-9-]+\.){2,}(?:internal|corp|intranet)\b/i, '内网主机名'],
  // 本机真实账号路径；<you> / %USERNAME% / 用户名 这类占位符不算
  [/[A-Za-z]:\\Users\\(?!<|you\\|your\\|username\\|用户名\\|%USERNAME%)[^\\\s'"]+\\/i, 'Windows 用户目录绝对路径'],
  [/(?:^|[\s"'(])\/(?:home|Users)\/(?!<|you\/|your\/|username\/|用户名\/|%USERNAME%)[A-Za-z0-9._-]+\//, 'Unix 用户目录绝对路径'],
  // 中国大陆手机号（只匹配独立数字串）
  [/(?<!\d)1[3-9]\d{9}(?!\d)/, '疑似真实手机号'],
  // 18 位身份证号（粗略形态；仅用于提示人工复核）
  [/(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)/, '疑似身份证号'],
  // 邮箱：示例/保留域名、localhost 放行
  [/[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b|localhost\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}/, '非示例邮箱'],
];

/**
 * 内部模板名/表名/字段名及其历史值的 SHA-256。
 * 这里故意只存哈希：公开仓库不再出现明文，但仍能在未来回归时命中。
 * 新增内部标识时：echo -n '<值>' | sha256sum，把哈希和“规则说明”加进来。
 */
const SENSITIVE_SHA256 = new Map([
  ['e4535128f5c55b90d5bf9a5f829ec0668d029890637c93cbd19d247d5bfe793a', '内部模板/业务标识'],
  ['667728b6c68885479b87355e0964f4da8060d1100f926946361806922d7dc715', '内部模板/业务标识'],
  ['93de20d20f5924d1cd1b0c9a94bf9cfb69a034fa0395e249ee3857ba13106b19', '内部模板/业务标识'],
  ['a7863dd556771190f1c84b713e75a747a011526b41acf2c743a03d5f2c84f327', '内部业务表名'],
  ['58138145fec5a00329f936f7dcd2d7d178ff6c6e09654196e04b262f689eb690', '内部业务表名'],
  ['8a97fa5e2eb7628242701b9a4d9e210b3761398b8e798d7429fdf24581954341', '内部业务表名'],
  ['1b4df7d01ea16b6049938b9d5ae9bfbf90064bc35566662b8d475d092717fcf7', '内部字段名'],
  ['b19518d96afeac876b560f247306382d928c5406d821ad315231bb4aea068008', '内部提交/版本标识'],
]);

/** 明确的说明性文字/占位符放行。 */
const ALLOW_LINE = [
  /环境变量/,
  /占位/,
  /示例/,
  /placeholder/i,
  /example\.(?:com|org|net|internal)\b/i,
  /\b(?:127\.0\.0\.1|0\.0\.0\.0|localhost)\b/,
  /scan-secrets/,
  /<DSH_HOME>|<盘符>|<用户>|<you>/,
  /PRIVATE_KEY.*(?:TEST|FIXTURE|FAKE)/i,
  /org\.eclipse|internal\.compiler/,
];

const TOKEN_SPLIT = /[\s"'`()\[\]{}<>;,:=/|，。；：、（）「」【】]+/u;
const sha256 = (value) => createHash('sha256').update(value).digest('hex');
const findings = [];

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    if (entry === '.git') continue;
    const abs = join(dir, entry);
    const stat = statSync(abs);
    if (stat.isDirectory()) {
      if (SKIP_DIRS.has(entry)) continue;
      walk(abs);
      continue;
    }
    if (!stat.isFile() || stat.size > MAX_BYTES) continue;
    const ext = entry.slice(entry.lastIndexOf('.')).toLowerCase();
    if (SKIP_EXT.has(ext)) continue;

    const rel = relative(ROOT, abs).replace(/\\/g, '/');
    const text = readFileSync(abs, 'utf8');
    const lines = text.split('\n');
    for (const [pattern, label] of RULES) {
      lines.forEach((line, index) => {
        if (!pattern.test(line)) return;
        if (ALLOW_LINE.some((allow) => allow.test(line))) return;
        findings.push({ file: rel, line: index + 1, label });
      });
    }
    // 内部标识：只做哈希比对，不输出命中内容。
    lines.forEach((line, index) => {
      if (ALLOW_LINE.some((allow) => allow.test(line))) return;
      const hit = line.split(TOKEN_SPLIT).some((token) => token.length >= 4 && SENSITIVE_SHA256.has(sha256(token)));
      if (hit) findings.push({ file: rel, line: index + 1, label: '内部业务标识（哈希比对）' });
    });
  }
}

console.log(`\ncpt-mcp · 公开推送前敏感信息门禁\n  根目录：${ROOT}\n`);
walk(ROOT);

if (findings.length === 0) {
  console.log('  ✓ 未发现明文凭据、真实 PII、内网地址、本机用户路径或内部业务标识');
  console.log('  ✓ 可以进入 push 前的人工复核\n');
  process.exit(0);
}

console.error(`  ✗ 命中 ${findings.length} 处，禁止推送：\n`);
for (const finding of findings) {
  console.error(`    ${finding.file}:${finding.line}  [${finding.label}]`);
}
console.error('\n  处置：删除/替换为占位符或环境变量；内部标识不得提交到公开分支。');
console.error('  历史中已推送的敏感内容不能只靠 force push 删除，需重建仓库或清理历史。\n');
process.exit(1);
