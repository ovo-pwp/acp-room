// 极简 CDP 探针：开无头 Chrome → 打开平台 → 截图 → 收控制台错误
import fs from 'node:fs';
const PORT = +(process.env.CDP_PORT || 9333);
const URL = process.argv[2] || 'http://127.0.0.1:8770/';
const OUT = process.argv[3] || '_shot.png';
const WAIT = +(process.argv[4] || 4000);
const sleep = ms => new Promise(r => setTimeout(r, ms));

let list;
for (let i = 0; i < 40; i++) {
  try { list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); break; }
  catch { await sleep(250); }
}
const pages = list.filter(x => x.type === 'page');
const t = pages[0];
const ws = new WebSocket(t.webSocketDebuggerUrl);
let id = 0; const pending = new Map(); const events = [];
const send = (m, p = {}) => new Promise(res => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method: m, params: p })); });
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result ?? m); pending.delete(m.id); }
  else if (m.method) events.push(m);
};
await new Promise(r => ws.onopen = r);
const ev = async x => (await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true })).result?.value;
await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 950, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: URL });
await sleep(WAIT);
const info = await ev(`JSON.stringify({title:document.title, agents:document.querySelectorAll('.agent').length, log:document.getElementById('log')?.children.length, cwd:document.getElementById('cwd')?.textContent, status:document.getElementById('status')?.textContent})`);
const s = await send('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync(OUT, Buffer.from(s.data, 'base64'));
const errs = events.filter(e => e.method === 'Runtime.exceptionThrown' || (e.method === 'Log.entryAdded' && e.params?.entry?.level === 'error' && !/favicon/.test(String(e.params?.entry?.url || ''))));
console.log('info=', info);
console.log('errors=', errs.length);
errs.slice(0, 5).forEach(e => console.log('  ', JSON.stringify(e.params).slice(0, 300)));
ws.close();
