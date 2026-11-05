// 量密度：把"太密了"变成数字。
// 不看截图（模型读不了图），只量几何。
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
const ev = async x => (await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true })).result?.value;
await send('Runtime.enable'); await send('Page.enable');
const WIDTHS = (process.argv[3] || '1400,1100,900').split(',').map(Number);
for (const W of WIDTHS) {
await send('Emulation.setDeviceMetricsOverride', { width: W, height: 950, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: URL });
await sleep(4000);

const out = await ev(`(() => {
  const px = s => parseFloat(s) || 0;
  const cs = el => getComputedStyle(el);
  const turns = [...document.querySelectorAll('.turn')];
  const last = turns.slice(-12);

  // 相邻两条消息之间的真实垂直间距
  const gaps = [];
  for (let i = 1; i < last.length; i++) {
    const a = last[i-1].getBoundingClientRect(), b = last[i].getBoundingClientRect();
    if (a.height && b.height) gaps.push(Math.round((b.top - a.bottom) * 10) / 10);
  }

  const sample = last[0];
  const s = sample ? cs(sample) : null;

  // 溢出检测：区分两种完全不同的情况
  //   A 真溢出：元素盒子超出父容器（会被裁掉 / 顶破布局）
  //   B 故意截断：text-overflow:ellipsis，scrollWidth 本来就大于 clientWidth，
  //     这是**正确**行为，报出来就是误报（上一版把 .p1-cur 误判了）
  const real = [], clipped = [];
  document.querySelectorAll('*').forEach(el => {
    const st = cs(el);
    if (st.display === 'none' || !el.clientWidth) return;
    // 表格分区（colgroup/thead/tbody/tr）天生就是表宽，拿来跟 table 比
    // 只会得到 +边框 的假数；transform 过的元素是纯视觉位移（滑出面板），
    // 不参与布局。这两类都不是"溢出"，排除。
    const tag = el.tagName;
    if (tag === 'COLGROUP' || tag === 'THEAD' || tag === 'TBODY' || tag === 'TR' || tag === 'COL') return;
    if (st.transform && st.transform !== 'none') return;

    const r = el.getBoundingClientRect();
    const pr = el.parentElement?.getBoundingClientRect();
    const label = (el.className || el.tagName).toString().trim().slice(0, 48);

    // A：盒子右边缘顶出父容器
    if (pr && pr.width > 0 && r.right > pr.right + 2) {
      real.push(label + ' 超出父级 +' + Math.round(r.right - pr.right) + 'px');
      return;
    }
    // B：内容被裁但没有省略号提示
    if (el.scrollWidth > el.clientWidth + 2) {
      const ell = st.textOverflow === 'ellipsis' || st.overflowX === 'auto' || st.overflowX === 'scroll';
      if (!ell) clipped.push(label + ' 被裁 +' + (el.scrollWidth - el.clientWidth) + 'px');
    }
  });

  // 布局列宽
  const cols = {};
  ['.rail', 'aside', '#log', '.p1-config', '.p14-row'].forEach(sel => {
    const el = document.querySelector(sel);
    if (el) { const r = el.getBoundingClientRect(); cols[sel] = Math.round(r.width) + 'x' + Math.round(r.height); }
  });

  return JSON.stringify({
    turnCount: turns.length,
    gapBetweenTurns: gaps,
    gapMedian: gaps.length ? gaps.sort((a,b)=>a-b)[Math.floor(gaps.length/2)] : null,
    turn: s ? {
      padding: s.padding, marginBottom: s.marginBottom,
      lineHeight: s.lineHeight, fontSize: s.fontSize,
      borderRadius: s.borderRadius
    } : null,
    bodyFont: cs(document.body).fontSize,
    logHeight: document.querySelector('#log')?.clientHeight,
    viewport: innerWidth + 'x' + innerHeight,
    cols,
    realOverflow: [...new Set(real)].slice(0, 10),
    clippedNoEllipsis: [...new Set(clipped)].slice(0, 10)
  });
})()`);

const d = JSON.parse(out);
console.log(`--- ${W}px ---`);
console.log(JSON.stringify({
  turns: d.turnCount,
  gapMedian: d.gapMedian,
  lineHeight: d.turn?.lineHeight,
  logH: d.logHeight,
  cols: d.cols,
  realOverflow: d.realOverflow,
  clippedNoEllipsis: d.clippedNoEllipsis
}, null, 2));
}
ws.close();
