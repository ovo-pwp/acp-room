// 量"光感"有没有真正落地：卡片明度、说话人色条、头像渐变、文字对比度
const PORT = +(process.env.CDP_PORT || 9333);
const URL = process.argv[2] || 'http://127.0.0.1:8770/';
const sleep = ms => new Promise(r => setTimeout(r, ms));

let list;
for (let i = 0; i < 40; i++) {
  try { list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); break; }
  catch { await sleep(250); }
}
const t = list.filter(x => x.type === 'page')[0];
const ws = new WebSocket(t.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise(res => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method: m, params: p })); });
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result ?? m); pending.delete(m.id); } };
await new Promise(r => ws.onopen = r);
const ev = async x => {
  const r = await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) return JSON.stringify({ error: String(r.exceptionDetails.text) });
  return r.result?.value;
};
await send('Runtime.enable'); await send('Page.enable');
await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true });
await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 950, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: URL });
await sleep(4500);

const expr = [
  '(() => {',
  '  const cs = el => getComputedStyle(el);',
  '  const ps = (el, p) => getComputedStyle(el, p);',
  '  const turns = [...document.querySelectorAll(".turn")];',
  '  const sample = turns.find(x => !x.classList.contains("human")) || turns[0];',
  '  const human = turns.find(x => x.classList.contains("human"));',
  '  const av = sample && sample.querySelector(".av");',
  '  const before = sample ? ps(sample, "::before") : null;',
  '  const bars = {};',
  '  turns.slice(-16).forEach(x => {',
  '    const nm = (x.querySelector(".who .nm") || {}).textContent || "?";',
  '    bars[nm.trim()] = ps(x, "::before").backgroundColor;',
  '  });',
  '  return JSON.stringify({',
  '    pageBgImage: cs(document.body).backgroundImage.slice(0,80),',
  '    cardInline: sample ? sample.getAttribute("style") : null,',
  '    cardBgImage: sample ? cs(sample).backgroundImage.slice(0,70) : null,',
  '    cardShadow: sample ? cs(sample).boxShadow.slice(0,80) : null,',
  '    barWidth: before ? before.width : null,',
  '    barHeight: before ? before.height : null,',
  '    barColor: before ? before.backgroundColor : null,',
  '    barOpacity: before ? before.opacity : null,',
  '    avatarBgImage: av ? cs(av).backgroundImage.slice(0,70) : null,',
  '    avatarShadow: av ? cs(av).boxShadow.slice(0,120) : null,',
  '    nameColor: sample ? cs(sample.querySelector(".who .nm")).color : null,',
  '    humanCardBg: human ? cs(human).backgroundImage.slice(0,70) : null,',
  '    barsBySpeaker: bars',
  '  }, null, 2);',
  '})()'
].join('\n');

console.log(await ev(expr));
ws.close();
