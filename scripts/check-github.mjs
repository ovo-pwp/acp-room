// 实证 GitHub 的锚点与 issue 模板
// 关键：GitHub 把 id 放在标题内部的 <a class="anchor"> 上，不在标题元素本身
const PORT = +(process.env.CDP_PORT || 9333);
const BASE = 'https://github.com/ovo-pwp/acp-room';
const sleep = ms => new Promise(r => setTimeout(r, ms));

let list;
for (let i = 0; i < 40; i++) {
  try { list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); break; }
  catch { await sleep(250); }
}
const ws = new WebSocket(list.filter(x => x.type === 'page')[0].webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise(res => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method: m, params: p })); });
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result ?? m); pending.delete(m.id); } };
await new Promise(r => ws.onopen = r);
const ev = async x => {
  const r = await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) return 'ERR ' + r.exceptionDetails.text;
  return r.result?.value;
};
await send('Runtime.enable'); await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1000, deviceScaleFactor: 1, mobile: false });

await send('Page.navigate', { url: BASE });
await sleep(7000);

console.log('=== 标题里真正的锚点（href 片段）===');
console.log(await ev(`(() => {
  const heads = [...document.querySelectorAll('article h2, article h3, .markdown-body h2, .markdown-body h3')];
  const rows = heads.map(h => {
    const a = h.querySelector('a[href^="#"], a.anchor');
    return { text: h.textContent.trim().slice(0, 28), fragment: a ? a.getAttribute('href') : null };
  }).filter(r => /模型|CLI|agent|参与/.test(r.text));
  return JSON.stringify(rows, null, 1);
})()`));

console.log('\n=== issue 模板页 ===');
await send('Page.navigate', { url: BASE + '/issues/new/choose' });
await sleep(6000);
console.log(await ev(`(() => {
  const body = document.body.innerText || '';
  const lines = body.split('\\n').map(s => s.trim()).filter(Boolean);
  const idx = lines.findIndex(s => /Get started|开始|模板|template/i.test(s));
  return JSON.stringify({
    url: location.href,
    hasBugForm: /Bug 报告|Bug report/i.test(body),
    hasFeatureForm: /功能建议|Feature request/i.test(body),
    hasLink1: /各家 CLI 的坑|连不上/.test(body),
    hasLink2: /模型和密钥从哪来|不知道模型/.test(body),
    snippet: lines.slice(Math.max(0, idx - 2), idx + 14)
  }, null, 1);
})()`));

ws.close();
