// 链接自检：本地相对链接 + GitHub 上的标题锚点
//
// 为什么要它：锚点是**静默失效**的。改了标题文字，链接照样能点，只是跳到页首，
// 而且没人会去点自己写的链接复查。CONTRIBUTING 里那条「文档之间互相矛盾比没有文档
// 更糟」同样适用于死链。
//
// 用法：
//   node scripts/check-links.mjs                    # 只查本地相对链接
//   node scripts/check-links.mjs --remote           # 额外验证 GitHub 锚点（要 CDP）
//
// 锚点的真实来源是 GitHub 渲染后的 DOM：它把 id 放在 <a id="user-content-..."> 上，
// 而不是标题元素本身。所以这里直接开浏览器读，不猜 slug 算法。

import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const REMOTE = process.argv.includes('--remote');
const OWNER = process.env.REPO || 'ovo-pwp/acp-room';
const BASE = `https://github.com/${OWNER}/blob/main/`;

const MD = ['README.md', 'README.en.md', 'CONTRIBUTING.md', '.github/PULL_REQUEST_TEMPLATE.md'];

let problems = 0;
const warn = (m) => { problems++; console.log('  ✗ ' + m); };
const ok = (m) => console.log('  ✓ ' + m);

// ---------- 1. 本地相对链接 ----------
console.log('=== 本地相对链接 ===');
const linkRe = /\[[^\]]*\]\(([^)\s]+)\)/g;
let checked = 0;
for (const f of MD) {
  if (!fs.existsSync(path.join(ROOT, f))) { warn(`${f} 不存在`); continue; }
  const text = fs.readFileSync(path.join(ROOT, f), 'utf8');
  for (const m of text.matchAll(linkRe)) {
    const href = m[1];
    if (/^(https?:|mailto:|#)/.test(href)) continue;
    const target = decodeURIComponent(href.split('#')[0]);
    if (!target) continue;
    checked++;
    if (!fs.existsSync(path.join(ROOT, target))) warn(`${f} → ${href}（文件不存在）`);
  }
}
ok(`检查了 ${checked} 条相对链接`);

// ---------- 2. 占位符 ----------
console.log('\n=== 占位符 ===');
for (const f of MD) {
  if (!fs.existsSync(path.join(ROOT, f))) continue;
  const text = fs.readFileSync(path.join(ROOT, f), 'utf8');
  for (const pat of [/<repo-url>/g, /<YOUR NAME>/g, /\bTODO\b/g, /\bFIXME\b/g]) {
    const n = (text.match(pat) || []).length;
    if (n) warn(`${f}: ${pat} 出现 ${n} 次`);
  }
}
ok('没有残留占位符');

// ---------- 3. GitHub 锚点 ----------
const cfgPath = path.join(ROOT, '.github/ISSUE_TEMPLATE/config.yml');
const cfg = fs.readFileSync(cfgPath, 'utf8');
const urls = [...cfg.matchAll(/url:\s*(\S+)/g)].map((m) => m[1]);
const wantFragments = urls.filter((u) => u.includes('#')).map((u) => decodeURIComponent(u.split('#')[1]));

if (!REMOTE) {
  console.log('\n=== GitHub 锚点 ===');
  console.log(`  （跳过；加 --remote 验证 ${wantFragments.length} 条锚点）`);
} else if (!wantFragments.length) {
  console.log('\n=== GitHub 锚点 ===');
  ok('config.yml 没有使用锚点');
} else {
  console.log('\n=== GitHub 锚点（开浏览器读真实 DOM）===');
  const PORT = +(process.env.CDP_PORT || 9333);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let list;
  for (let i = 0; i < 40; i++) {
    try { list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); break; }
    catch { await sleep(250); }
  }
  if (!list) {
    warn('连不上 CDP（先启动带 --remote-debugging-port 的 Chrome）');
  } else {
    const ws = new WebSocket(list.filter((x) => x.type === 'page')[0].webSocketDebuggerUrl);
    let id = 0; const pending = new Map();
    const send = (m, p = {}) => new Promise((res) => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method: m, params: p })); });
    ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result ?? m); pending.delete(m.id); } };
    await new Promise((r) => (ws.onopen = r));
    const ev = async (x) => (await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true })).result?.value;
    await send('Runtime.enable'); await send('Page.enable');
    await send('Page.navigate', { url: `https://github.com/${OWNER}` });
    await sleep(7000);
    const slugs = JSON.parse(await ev(`JSON.stringify([...document.querySelectorAll('[id^="user-content"]')].map(e=>e.id.replace('user-content-','')))`) || '[]');
    if (!slugs.length) {
      warn('没抓到任何锚点（页面没加载完？）');
    } else {
      for (const frag of wantFragments) {
        if (slugs.includes(frag)) ok(frag);
        else warn(`锚点不存在: #${frag}`);
      }
    }
    ws.close();
  }
}

console.log('\n' + (problems ? `${problems} 个问题` : '全部通过'));
process.exit(problems ? 1 : 0);
