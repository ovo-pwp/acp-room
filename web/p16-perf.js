/* P16（claude 交付）算力对比面板 —— 让每个 agent 的真实算力看得见。
   数据源两级：
     GET /api/health（P13 已有）→ 模型 / 中位耗时 p50_ms / 累计 token / 颜色
     GET /api/bench   （pi 接，当前 404）→ 吞吐 tok/s / 上下文
   两个接口都降级：拿不到就占位"未就绪"，绝不影响页面其余部分。
   关键洞察（pi 实测）写死在面板底部：四个模型答题能力接近，差在推理档位。
   自包含：挂载点自己找（p1-config 面板之后），样式内嵌，零依赖。 */
(() => {
  'use strict';

  const esc = window.esc || ((s) => String(s == null ? '' : s)
    .replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));

  function fmtTok(n) {
    if (n == null) return '—';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k';
    return String(n);
  }
  function fmtMs(ms) {
    if (ms == null) return '—';
    return (ms / 1000).toFixed(1) + 's';
  }

  /* ---- 样式（内嵌，不新增 css 文件） ---- */
  const style = document.createElement('style');
  style.id = 'p16-style';
  style.textContent = `
.p16-perf { border-top: 1px solid var(--line); padding: 10px; font-size: 12px; }
.p16-head { display: flex; justify-content: space-between; align-items: center;
  color: var(--dim); font-weight: 600; letter-spacing: .3px; cursor: pointer; user-select: none; }
.p16-head:hover { color: var(--accent); }
.p16-arrow { color: var(--dim); width: 10px; flex: none; }
.p16-body { margin-top: 8px; display: flex; flex-direction: column; gap: 8px; }
/* 6 列塞进 300px 面板 = 溢出 171px，内容直接被裁掉。
   治法是减列而不是加滚动条：面板窄，横向滚动条在鼠标滚轮下很难用，
   而且用户要的是"别那么密"，不是"能滚着看更密的东西"。
   现在 3 列：谁 / 吞吐 / 耗时；模型名降级成名字下面的小字，
   上下文和累计 token 进 title，需要的悬停就有。 */
.p16-table { width: 100%; border-collapse: collapse; font-size: 11px; table-layout: fixed; }
.p16-table th, .p16-table td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--line); }
.p16-table th { color: var(--dim); font-weight: 600; font-size: 10px; white-space: nowrap; }
.p16-table td { color: var(--text); font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p16-table col.c-who { width: 42%; }
.p16-table col.c-num { width: 29%; }
.p16-who { display: block; overflow: hidden; text-overflow: ellipsis; }
.p16-table .p16-model { display: block; font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 9.5px; color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p16-table .p16-na { color: var(--dim); }
.p16-table .p16-top { color: var(--ok); font-weight: 700; }
.p16-note { font-size: 10px; color: var(--dim); }
.p16-insight { border: 1px solid var(--accent); background: #1f6feb14; color: var(--text);
  border-radius: 6px; padding: 6px 9px; font-size: 11px; }
.p16-insight b { color: var(--accent); }
.p16-placeholder { color: var(--warn); font-size: 11px; padding: 6px 0; }
`;
  document.head.appendChild(style);

  /* ---- 面板 DOM ---- */
  let panel = null, body = null, arrow = null, opened = true;
  let healthData = null;   // 最近一次 /api/health
  let benchData = null;    // 最近一次 /api/bench（没就绪为 null）
  let healthErr = null, benchErr = null;

  function mount() {
    if (document.getElementById('p16-perf')) return;
    const aside = document.querySelector('aside');
    if (!aside) return;
    const anchor = document.querySelector('.p1-config') || document.getElementById('agents');
    if (!anchor) return;
    panel = document.createElement('section');
    panel.id = 'p16-perf';
    panel.className = 'p16-perf';
    panel.innerHTML = `
      <div class="p16-head" title="展开/收起">
        <span class="p16-arrow">▾</span><span>算力对比</span>
      </div>
      <div class="p16-body"></div>`;
    body = panel.querySelector('.p16-body');
    arrow = panel.querySelector('.p16-arrow');
    panel.querySelector('.p16-head').addEventListener('click', () => {
      opened = !opened;
      body.style.display = opened ? '' : 'none';
      arrow.textContent = opened ? '▾' : '▸';
    });
    if (anchor.nextElementSibling) aside.insertBefore(panel, anchor.nextElementSibling);
    else aside.appendChild(panel);
  }

  function benchRow(aid) {
    if (!benchData) return null;
    const rows = Array.isArray(benchData) ? benchData : (benchData.rows || []);
    return rows.find(r => r.agent === aid || r.id === aid) || null;
  }

  function render() {
    if (!body) return;
    if (healthErr && !healthData) {
      body.innerHTML = `<div class="p16-placeholder">算力面板未就绪（/api/health 不可用：${esc(healthErr)}）。</div>`;
      return;
    }
    const agents = (healthData && healthData.agents) || [];
    if (!agents.length) {
      body.innerHTML = `<div class="p16-placeholder">/api/health 没有 agent 数据。</div>`;
      return;
    }
    const tokCol = benchData ? '' : '（待 /api/bench）';
    let rows = '';
    for (const a of agents) {
      const b = benchRow(a.id);
      const tok = b ? (b.tok_s != null ? b.tok_s : b.tokens_per_sec) : null;
      const ctx = b ? (b.context || b.ctx) : null;
      rows += `<tr title="上下文 ${ctx != null ? esc(String(ctx)) : '—'} · 累计 ${fmtTok(a.tokens_total)}">
        <td><span class="p16-who"><span style="color:${esc(a.color || '#888')}">●</span> ${esc(a.name || a.id)}</span><span class="p16-model">${esc((a.config && a.config.model) || '—')}</span></td>
        <td class="${tok != null ? 'p16-top' : 'p16-na'}">${tok != null ? tok + ' tok/s' : '—'}</td>
        <td>${fmtMs(a.p50_ms)}</td>
      </tr>`;
    }
    body.innerHTML = `
      <table class="p16-table">
        <colgroup><col class="c-who"><col class="c-num"><col class="c-num"></colgroup>
        <thead><tr>
          <th>agent / 模型</th><th>吞吐 ${tokCol}</th><th>中位耗时</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${benchData ? '' : `<div class="p16-note">吞吐列待 /api/bench 就绪后自动补上（现在显示 —）。</div>`}
      <div class="p16-insight">💡 四个模型答题能力接近（都答对了 13/27 这类难题），但耗时差 10 倍——<b>差在推理档位，不在模型本身</b>。</div>`;
  }

  async function refresh() {
    try {
      const r = await fetch('/api/health', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      healthData = await r.json();
      healthErr = null;
    } catch (e) {
      healthErr = e && e.message ? e.message : String(e);
    }
    try {
      const r = await fetch('/api/bench', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      benchData = await r.json();
      benchErr = null;
    } catch (e) {
      benchData = null;
      benchErr = e && e.message ? e.message : String(e);
    }
    render();
  }

  mount();
  refresh();
  setInterval(refresh, 30000);
})();
