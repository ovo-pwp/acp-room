/* P7（claude 交付）工具调用卡片 + 审批弹窗"为什么" —— 抄 OpenBot 信息架构。
   - 不碰 app.js：app.js 照旧往 .tools 里写 <span class="tool">，本文件用
     MutationObserver 把这些 chip 升级成卡片，chip 留在卡片里当数据源（隐藏）。
   - decision 来自 P6 审计（window.p7Audit，由 p7-audit.js 维护）；没有就显示"未知"。
   - showPermission 只做包装（原函数照跑），弹窗里补一行"为什么：<触发规则>"。 */
(() => {
  'use strict';

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

  /* ================= 工具调用卡片 ================= */

  const created = new WeakMap();   // card -> performance.now()
  const doneSecs = new WeakMap();  // card -> 结束时的耗时（冻结，不再变）

  function statusOf(chip) {
    const st = (chip.className || '').split(/\s+/).find((c) => c && c !== 'tool');
    return st || 'pending';
  }

  function agentOf(card) {
    const turn = card.closest('.turn');
    return turn ? (turn.id || '').replace(/^live-/, '') : '';
  }

  function renderCard(card, chip) {
    const title = chip.dataset.title || (chip.textContent || '').split(' · ')[0] || '';
    const status = statusOf(chip);
    const agent = agentOf(card);

    let decision = null, kind = null;
    const store = window.p7Audit || null;
    if (store) {
      decision = store.decision(agent, title);
      kind = store.kindOf(agent, title);
    }

    if (!created.has(card)) created.set(card, performance.now());
    let secs;
    if (doneSecs.has(card)) {
      secs = doneSecs.get(card);
    } else if (/completed|ok|failed|error/.test(status)) {
      secs = (performance.now() - created.get(card)) / 1000;
      doneSecs.set(card, secs);
    } else {
      secs = (performance.now() - created.get(card)) / 1000;
    }

    const ok = /completed|ok/.test(status);
    const bad = /failed|error/.test(status);
    const stLabel = ({ completed: 'ok', in_progress: 'running', pending: 'pending',
      failed: 'failed', running: 'running', ok: 'ok', error: 'error' })[status] || status;
    // P16 修：P10 策略引擎后 decision 实际值是 allow/deny/escalate（core/policy.py
    // DecisionKind），老的 auto_allow 已不存在；两套取值都认，避免回到全灰。
    const dcls = ({ allow: 'ok', auto_allow: 'ok', user_allowed: 'accent',
      escalate: 'warn', deny: 'err', user_denied: 'err', ask: 'warn' })[decision] || 'dim';

    let rule = null;
    if (store && store.ruleFor) rule = store.ruleFor(agent, title);

    let content = card.querySelector('.p7-cbody');
    if (!content) {
      content = document.createElement('div');
      content.className = 'p7-cbody';
      card.appendChild(content);
    }
    content.innerHTML = `
      <div class="p7-head">
        <span class="p7-ico">🔧</span>
        <span class="p7-kind">${esc(kind || status)}</span>
        <span class="p7-badge p7-d-${dcls}">${esc(decision || '未知')}</span>
      </div>
      <div class="p7-t">${esc(title)}</div>
      ${rule ? `<div class="p7-rule" title="${esc(rule)}">${esc(rule.length > 90 ? rule.slice(0, 90) + '…' : rule)}</div>` : ''}
      <div class="p7-st ${ok ? 'ok' : (bad ? 'bad' : (/running|in_progress/.test(status) ? 'run' : ''))}">
        ${ok ? '✓ ok' : (bad ? '✗ ' + stLabel : '… ' + stLabel)}<span class="p7-secs"> · ${secs.toFixed(1)}s</span>
      </div>`;
  }

  function sweep() {
    const log = document.getElementById('log');
    if (!log) return;
    log.querySelectorAll('.tools').forEach((box) => {
      box.querySelectorAll('span.tool').forEach((chip) => {
        let card = chip.closest('.p7-tool-card');
        if (!card) {
          card = document.createElement('div');
          card.className = 'p7-tool-card';
          chip.parentNode.insertBefore(card, chip);
          card.appendChild(chip);      // chip 留作数据源（app.js 还会往里写）
          chip.style.display = 'none';
        }
        renderCard(card, chip);
      });
    });
  }

  let t = null;
  function schedule() { clearTimeout(t); t = setTimeout(sweep, 60); }  // 防 chunk 风暴

  function observe() {
    const log = document.getElementById('log');
    if (!log) { setTimeout(observe, 300); return; }
    new MutationObserver(() => schedule())
      .observe(log, { childList: true, subtree: true, characterData: true, attributes: true });
    sweep();
  }

  // 审计轮询到了新数据 → 卡片上的 decision 徽章跟着刷新
  document.addEventListener('p7:audit', schedule);

  /* ================= 审批弹窗补"为什么" ================= */

  const origShow = window.showPermission;
  let lastPerm = null;
  if (typeof origShow === 'function') {
    window.showPermission = function (m) {
      origShow(m);                     // 原逻辑照跑：标题/选项/按钮一个不少
      lastPerm = m;
      const desc = document.getElementById('perm-desc');
      if (!desc) return;
      let why = document.getElementById('p7-perm-why');
      if (!why) {
        why = document.createElement('p');
        why.id = 'p7-perm-why';
        why.className = 'p7-why';
        desc.after(why);
      }
      why.dataset.pid = m.id;          // 防旧请求回来时覆盖新弹窗
      why.textContent = '为什么：查询中…';
      fillWhy(m, why);
    };
  }

  // 审计轮询到了新数据 → 已打开的弹窗"为什么"行跟着补上真实规则
  document.addEventListener('p7:audit', () => {
    if (!lastPerm) return;
    const why = document.getElementById('p7-perm-why');
    if (why && why.dataset.pid === lastPerm.id) fillWhy(lastPerm, why);
  });

  let agentsCache = null;
  async function agentPolicy(id) {
    if (agentsCache === null) {
      try {
        const r = await fetch('/api/agents');
        agentsCache = r.ok ? await r.json() : [];
      } catch { agentsCache = []; }
    }
    const a = (agentsCache || []).find((x) => x.id === id);
    return a ? a.permission : null;
  }

  async function fillWhy(m, why) {
    let rule = null;
    // 首选：P6 审计里这条 permission 事件的 rule
    const store = window.p7Audit || null;
    if (store && store.permRule) rule = (store.permRule(m.agent) || {}).rule || null;
    // 兜底：/api/agents 里的权限策略
    if (!rule) {
      const policy = await agentPolicy(m.agent);
      if (policy) {
        rule = `config.agents[].permission == ${policy}` +
          (policy === 'ask' ? '（ask = 需要人工审批）' : '');
      }
    }
    if (!rule) rule = '未知（审计接口与 /api/agents 均不可用）';
    // 弹窗可能已经关了/换成新的：按 pid 对账，只写给自己的那一次
    if (why.dataset.pid === m.id) why.textContent = '为什么：' + rule;
  }

  /* ================= 启动 ================= */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', observe);
  } else {
    observe();
  }
})();
