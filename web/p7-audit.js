/* P7（claude 交付）活动/审计侧栏 —— 抄 OpenBot 的 activity 流。
   - GET /api/audit?limit=20 每 5 秒轮询，最近 20 条倒序展示
   - 降级：接口 404 / 网络错误 / enabled=false 一律显示占位，绝不许白屏
   - 同时把审计行喂给 window.p7Audit 供 p7-cards.js 显示 decision（P6 数据）
   自包含：面板 DOM 自己建（index.html 只加 script 标签），CSS 自己注入。 */
(() => {
  'use strict';

  const POLL_MS = 5000;
  const LIMIT = 20;

  /* ---- 注入样式（幂等） ---- */
  if (!document.getElementById('p7-css')) {
    const link = document.createElement('link');
    link.id = 'p7-css';
    link.rel = 'stylesheet';
    link.href = '/static/p7-cards.css';
    document.head.appendChild(link);
  }

  const esc = window.esc || ((s) => String(s == null ? '' : s)
    .replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));

  /* ---- 共享审计 store：agent+title -> {decision, rule, kind} ---- */
  const _map = new Map();
  const _perm = new Map();   // agent -> {decision, rule}（最近一条 permission 行）

  function key(agent, title) { return (agent || '') + '\u0001' + (title || ''); }

  const p7Audit = {
    update(rows) {
      for (const r of rows || []) {
        const e = { decision: r.decision || '?', rule: r.rule || '', kind: r.tool || '' };
        _map.set(key(r.agent, r.title), e);
        if (r.event === 'permission') _perm.set(r.agent, e);
      }
    },
    decision(agent, title) {
      const e = _map.get(key(agent, title));
      return e ? e.decision : null;
    },
    kindOf(agent, title) {
      const e = _map.get(key(agent, title));
      return e && e.kind ? e.kind : null;
    },
    ruleFor(agent, title) {   // P16：工具卡片显示触发规则
      const e = _map.get(key(agent, title));
      return e && e.rule ? e.rule : null;
    },
    permRule(agent) { return _perm.get(agent) || null; },
  };
  window.p7Audit = p7Audit;

  /* ---- 面板 DOM（自己建，不动 index.html 结构） ---- */
  const panel = document.createElement('aside');
  panel.id = 'p7-audit';
  panel.innerHTML = `
    <div class="p7-aud-head">
      <span class="p7-aud-title">活动 · 审计</span>
      <button class="p7-aud-close" title="收起">»</button>
    </div>
    <div class="p7-aud-body"></div>`;
  const tab = document.createElement('div');
  tab.id = 'p7-aud-tab';
  tab.textContent = '« 活动';
  document.body.appendChild(panel);
  document.body.appendChild(tab);

  function setCollapsed(on) {
    panel.classList.toggle('p7-collapsed', on);
    tab.classList.toggle('show', on);
    // 侧栏展开时给 body 留出 300px（见 style.css），否则正文和输入框被压在这一栏底下
    document.body.classList.toggle('p7-rail-open', !on);
    try { localStorage.setItem('p7-audit-open', on ? '0' : '1'); } catch { /* ignore */ }
  }
  panel.querySelector('.p7-aud-close').addEventListener('click', () => setCollapsed(true));
  tab.addEventListener('click', () => setCollapsed(false));
  // 没有存过偏好时默认收起：健康的 p13 面板已经把审计信息收进去了，
  // 两块都摊开会把正文挤到 150px 宽（见 style.css 的 rail 规则）
  let collapsed0 = true;
  try {
    const v = localStorage.getItem('p7-audit-open');
    collapsed0 = v === null ? true : v === '0';
  } catch { /* ignore */ }
  setCollapsed(collapsed0);

  const body = panel.querySelector('.p7-aud-body');

  function placeholder(title, hint) {
    return `<div class="p7-placeholder">
      <div class="p7-ph-title">${esc(title)}</div>
      <div>${esc(hint || '')}</div>
    </div>`;
  }

  function dcls(decision) {
    // P16 修：与 p7-cards.js 同步——P10 后 decision 是 allow/deny/escalate
    return ({ allow: 'ok', auto_allow: 'ok', user_allowed: 'accent',
      escalate: 'warn', deny: 'err', user_denied: 'err', ask: 'warn' })[decision] || 'dim';
  }

  /* P18：耗时角标的颜色。判据和发言卡片(.who .slow)保持一致——
     两处对「慢」的口径不一样，用户就会问到底信哪个。 */
  if (!document.getElementById('p7-dur-css')) {
    const st = document.createElement('style');
    st.id = 'p7-dur-css';
    st.textContent = `
.p7-dur { color: var(--dim2); font-size: 10px; margin-left: auto;
  font-variant-numeric: tabular-nums; }
.p7-dur.warn { color: var(--warn); }
.p7-dur.err { color: var(--err); }
`;
    document.head.appendChild(st);
  }

  function dclsDur(ms) {
    return ms >= 60000 ? 'err' : (ms >= 20000 ? 'warn' : '');
  }

  function renderRows(data) {
    const rows = (data.rows || []).slice().reverse();  // 最新在前
    const total = (data.stats && data.stats.total != null) ? data.stats.total : (data.count || 0);
    let html = `<div class="p7-aud-meta">共 ${total} 条 · 显示最近 ${rows.length} 条</div>`;
    if (!rows.length) {
      html += `<div class="p7-empty">审计已启用，还没有记录。</div>`;
    }
    for (const r of rows) {
      const t = new Date((r.ts || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false });
      // P18：每条判定带上它实际耗了多久。之前这一栏只有事件名，
      // 「谁慢」还得切到面板去看对时间戳——而审计行本来就有 ms / ttft_ms。
      let dur = '';
      if (r.ms) {
        dur = `<span class="p7-dur ${dclsDur(r.ms)}" title="这一条跑了 ${(r.ms / 1000).toFixed(1)}s`
          + (r.ttft_ms ? `，其中第一个字等了 ${(r.ttft_ms / 1000).toFixed(1)}s` : '')
          + `">${(r.ms / 1000).toFixed(1)}s</span>`;
      } else if (r.ttft_ms) {
        dur = `<span class="p7-dur">首字 ${(r.ttft_ms / 1000).toFixed(1)}s</span>`;
      }
      html += `<div class="p7-row ${r.status === 'error' ? 'p7-err' : ''}" title="${esc(r.rule || '')}">
        <div class="p7-row-top">
          <span class="p7-t">${t}</span>
          <span class="p7-a">${esc(r.agent)}</span>
          <span class="p7-e">${esc(r.event)}</span>
          <span class="p7-badge p7-d-${dcls(r.decision)}">${esc(r.decision || '?')}</span>
          ${dur}
        </div>
        <div class="p7-row-title">${esc(r.title || r.tool || '')}</div>
      </div>`;
    }
    body.innerHTML = html;
  }

  async function refresh() {
    let data;
    try {
      const r = await fetch(`/api/audit?limit=${LIMIT}`, { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      data = await r.json();
    } catch (e) {
      // 降级路径 1：接口没了（404/断网/后端未重启）——占位，不崩
      body.innerHTML = placeholder('审计不可用',
        `接口请求失败（${e && e.message ? e.message : e}）。P6 接口未就绪或后端未重启，页面其余功能不受影响。`);
      return;
    }
    if (!data || data.enabled !== true) {
      // 降级路径 2：P6 没接入 —— 占位，不崩
      body.innerHTML = placeholder('审计未启用',
        (data && data.hint) || 'P6 审计尚未接入 core/audit.py。');
      return;
    }
    p7Audit.update(data.rows || []);
    renderRows(data);
    document.dispatchEvent(new CustomEvent('p7:audit'));  // 通知工具卡片刷新 decision
  }

  refresh();
  setInterval(refresh, POLL_MS);
})();
