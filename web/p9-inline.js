/* P9 内联审批（claude 交付）—— 抄 OpenBot inline-approval.tsx：
   "A pending approval, drawn in the conversation where the action it would have been."
   - {type:"permission"} 不再弹全屏 #perm 模态，在对话里就地画卡片
     （守卫在 app.js 的调用处：window.P9_INLINE && window.p9Inline 时走这里）
   - 卡片位置：触发 agent 的 live turn 块内（#live-<agent> 的 .body）；
     没有 live 块（agent 还没 turn_start / 测试场景）就挂 #log 末尾
   - 同时多条：按 m.id 区分，各自独立；决定后变结果态，不再可点
   - 按钮分档照 OpenBot：从 ACP options 的 kind 映射（allow_once/allow_always/
     reject_once/reject_always），没有 reject 档时补一个"拒绝"(__cancel__)
   - 策略行接 P7 的输入（window.p7Audit.permRule），兜底 /api/agents，再兜底"未知"
   - 决定走 POST /api/permission（不依赖 app.js 内部的 ws 变量）
   自包含：样式内嵌 <style>（本单只许新增这一个文件）。 */
(() => {
  'use strict';

  window.P9_INLINE = window.P9_INLINE !== false;   // 默认开内联；手动设 false 可回退模态

  /* ---- 样式（内嵌，不新增 css 文件） ---- */
  const style = document.createElement('style');
  style.id = 'p9-style';
  style.textContent = `
.p9-approval {
  border: 1px solid var(--warn); border-left: 3px solid var(--warn);
  background: var(--panel2); border-radius: 8px; padding: 10px 12px;
  margin: 8px 0; max-width: 480px;
}
.p9-approval .p9-head { font-size: 12px; font-weight: 700; color: var(--warn);
  display: flex; gap: 8px; align-items: baseline; }
.p9-round { color: var(--dim); font-weight: 400; font-size: 10px; }
.p9-approval .p9-line { font-size: 13px; margin-top: 4px; word-break: break-all; }
.p9-approval .p9-policy { font-size: 11px; color: var(--dim); margin-top: 4px; }
.p9-approval .p9-opts { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.p9-approval .p9-btn {
  background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 5px 12px; cursor: pointer; font: inherit; font-size: 12px;
}
.p9-approval .p9-btn:hover { border-color: var(--accent); color: var(--accent); }
.p9-approval .p9-btn.p9-deny { color: var(--err); }
.p9-approval .p9-btn.p9-deny:hover { border-color: var(--err); }
.p9-approval.p9-result { border-color: var(--line); border-left-color: var(--ok);
  background: var(--panel); }
.p9-approval.p9-result .p9-head { color: var(--text); }
.p9-approval .p9-when { font-size: 10px; color: var(--dim); margin-top: 4px; }
`;
  document.head.appendChild(style);

  const _esc = window.esc || ((s) => String(s == null ? '' : s)
    .replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));
  const esc = (s) => _esc(String(s == null ? '' : s));   // app.js 的 esc 不转数字（turn_seq=42 会炸），这里兜住

  // OpenBot 按钮分档：ACP 标准四种 kind → 中文档位
  const KIND_LABEL = {
    allow_once: '允许一次', allow_always: '总是允许',
    reject_once: '拒绝', reject_always: '拒绝并记住',
  };

  function agentName(id) {
    try {
      if (typeof state !== 'undefined' && state && state.agents) {
        const a = state.agents.find((x) => x.id === id);
        if (a) return a.name;
      }
    } catch { /* ignore */ }
    return id;
  }

  function cardHtml(m, name) {
    const opts = (m.options || []).map((o) => {
      const label = KIND_LABEL[o.kind] || o.name || o.optionId;
      return `<button class="p9-btn" data-opt="${esc(o.optionId)}">${esc(label)}</button>`;
    });
    if (!(m.options || []).some((o) => /reject/.test(o.kind || ''))) {
      opts.push(`<button class="p9-btn p9-deny" data-opt="__cancel__">拒绝</button>`);
    }
    const round = m.turn_seq != null ? `<span class="p9-round">第 ${esc(m.turn_seq)} 轮</span>` : '';
    return `<div class="p9-approval" id="p9-${esc(m.id)}">
      <div class="p9-head">⏸ 等待你的批准${round}</div>
      <div class="p9-line">${esc(name)} 想执行：${esc(m.tool || '工具调用')}</div>
      <div class="p9-policy">策略：<span class="p9-rule">查询中…</span></div>
      <div class="p9-opts">${opts.join('')}</div>
    </div>`;
  }

  function wire(card, m) {
    card.querySelectorAll('.p9-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const opt = btn.dataset.opt;
        const approved = !/reject|__cancel__/.test(opt);
        card.classList.add('p9-result');
        card.innerHTML = `
          <div class="p9-head">${approved ? '✅ 已批准' : '🚫 已拒绝'}</div>
          <div class="p9-line">${esc(agentName(m.agent))} · ${esc(m.tool || '工具调用')}</div>
          <div class="p9-when">${new Date().toLocaleTimeString('zh-CN', { hour12: false })}</div>`;
        // 决定发给后端（乐观 UI：本地先变结果态，后端失败不回滚——决定已经做了）
        fetch('/api/permission', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: m.id, optionId: opt }),
        }).catch(() => { /* 后端没响应不阻塞界面 */ });
      });
    });
  }

  /* ---- 策略行：接 P7 的输入 ---- */
  let agentsCache = null;
  async function fillPolicy(card, m) {
    let rule = null;
    const store = window.p7Audit || null;
    if (store && store.permRule) rule = (store.permRule(m.agent) || {}).rule || null;
    if (!rule) {
      try {
        if (agentsCache === null) {
          const r = await fetch('/api/agents');
          agentsCache = r.ok ? await r.json() : [];
        }
        const a = (agentsCache || []).find((x) => x.id === m.agent);
        if (a && a.permission) rule = `config.agents[].permission == ${a.permission}`;
      } catch { /* ignore */ }
    }
    if (!rule) rule = '未知（审计与 /api/agents 均不可用）';
    const el = card.querySelector('.p9-rule');
    if (el && !card.classList.contains('p9-result')) el.textContent = rule;
  }

  // 审计轮询到了新数据 → 未决卡片的策略行跟着刷新
  document.addEventListener('p7:audit', () => {
    document.querySelectorAll('.p9-approval:not(.p9-result)').forEach((c) => {
      if (c.__p9m) fillPolicy(c, c.__p9m);
    });
  });

  function mount(m) {
    if (!m || !m.id) return;
    if (document.getElementById('p9-' + m.id)) return;   // 防重放
    const name = agentName(m.agent);
    // 位置：触发 agent 的 live turn 块内；没有 live 块就挂 #log 末尾
    const host = document.getElementById('live-' + m.agent);
    const holder = host ? (host.querySelector('.body') || host)
                        : document.getElementById('log');
    if (!holder) return;
    const wrap = document.createElement('div');
    wrap.innerHTML = cardHtml(m, name);
    const card = wrap.firstElementChild;
    card.__p9m = m;
    holder.appendChild(card);
    wire(card, m);
    fillPolicy(card, m);
    const log = document.getElementById('log');
    if (log) log.scrollTop = log.scrollHeight;
  }

  window.p9Inline = mount;   // app.js 调用处的守卫会调这个
})();
