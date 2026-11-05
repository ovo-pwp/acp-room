/* P18 慢在哪 —— 把审计里的真实耗时变成一句能行动的话。
   为什么单独一栏：P16 的「算力对比」回答的是**模型吞吐**（tok/s，来自 bench 探针），
   它答不了「我刚才那句等了 4 分钟是谁造成的」。这两个问题经常被当成一个，
   于是有人拿 tok/s 排瓶颈、有人拿最长一轮排瓶颈，两边都能"证明"自己怀疑的那个人。
   这一栏只报**全体发言实测**：中位 / p90 / 样本数，样本不够就明说不排名。

   数据源：GET /api/perf（审计 + turns.jsonl 一起算）
   降级：接口 404（后端没重启）→ 占位，页面其余部分不受影响。
   自包含：面板自己建、样式自己注、零依赖。 */
(() => {
  'use strict';

  const MIN_HINT = '条样本';

  const esc = window.esc || ((s) => String(s == null ? '' : s)
    .replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));

  if (!document.getElementById('p18-css')) {
    const st = document.createElement('style');
    st.id = 'p18-css';
    st.textContent = `
.p18 { border-top: 1px solid var(--line); padding: 10px; font-size: 12px; }
.p18-head { display: flex; justify-content: space-between; align-items: center;
  color: var(--dim); font-weight: 600; cursor: pointer; user-select: none; }
.p18-head:hover { color: var(--accent); }
.p18-arrow { color: var(--dim); width: 10px; flex: none; }
.p18-body { margin-top: 8px; }
/* 4 列：窄侧栏放 6 列必然溢出（p16 踩过）。最长和首字进 title，悬停就有。 */
.p18-table { width: 100%; border-collapse: collapse; font-size: 11px; table-layout: fixed; }
.p18-table th, .p18-table td { text-align: left; padding: 4px 6px;
  border-bottom: 1px solid var(--line); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p18-table th { color: var(--dim); font-weight: 600; font-size: 10px; }
.p18-table col.c-who { width: 40%; }
.p18-table td.num { font-variant-numeric: tabular-nums; }
.p18-table td.dim { color: var(--dim2); }
.p18-verdict { font-size: 11px; color: var(--text); margin-bottom: 6px; }
.p18-verdict b { color: var(--accent); }
.p18-warn { color: var(--warn); }
.p18-note { font-size: 10px; color: var(--dim); margin-top: 6px; }
.p18-placeholder { color: var(--warn); font-size: 11px; }
/* 底部条上的「本条还剩 Ns」。只在设了预算且正在跑时出现，平时不占位。 */
#budget { color: var(--dim2); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
#budget.tight { color: var(--warn); }
#budget.out { color: var(--err); }
#resume { display: none; }
#resume.show { display: inline-block; }
`;
    document.head.appendChild(st);
  }

  /* ---- 面板 ---- */
  let panel = null, body = null, arrow = null, opened = false;
  let summary = null, err = null, totalRows = 0, withTtft = 0;
  // 状态由 app.js 在每次 renderAll 时递进来。它那个 `state` 是脚本顶层的 `let`，
  // 顶层 let/const 不挂到 window，所以本文件读不到 window.state。
  let lastState = null;

  function mount() {
    if (document.getElementById('p18-latency')) return;
    const aside = document.querySelector('aside');
    if (!aside) return;
    panel = document.createElement('section');
    panel.id = 'p18-latency';
    panel.className = 'p18';
    panel.innerHTML = `
      <div class="p18-head" title="展开/收起">
        <span class="p18-arrow">▸</span><span>慢在哪（全体实测）</span>
      </div>
      <div class="p18-body"></div>`;
    body = panel.querySelector('.p18-body');
    arrow = panel.querySelector('.p18-arrow');
    panel.querySelector('.p18-head').addEventListener('click', () => {
      opened = !opened;
      body.style.display = opened ? '' : 'none';
      arrow.textContent = opened ? '▾' : '▸';
      try { localStorage.setItem('p18-open', opened ? '1' : '0'); } catch { /* ignore */ }
      if (opened) load(0);   // 人刚点开，要的是新数，别拿 2 秒防抖挡住
    });
    try { opened = localStorage.getItem('p18-open') !== '0'; } catch { /* ignore */ }
    body.style.display = opened ? '' : 'none';
    arrow.textContent = opened ? '▾' : '▸';
    const anchor = document.getElementById('p16-perf');
    if (anchor && anchor.parentNode === aside) aside.insertBefore(panel, anchor.nextSibling);
    else aside.appendChild(panel);
    // 底部条上的两个元素**建页时就摆好**，不等第一次状态推送。
    // 实测踩过：只靠 renderAll 里的 onState 钩子创建，页面加载那一次状态推送
    // 没走到钩子，`#budget` 就一直不存在，后面每次推送都只在更新两个游离节点。
    ensureBar();
    // P17 会把这一栏搬进右侧抽屉的「慢在哪」分页。抽屉里切回来时元素从
    // display:none 变可见（offsetParent 不再是 null），这里借它顺手刷新；
    // 搬动本身不用管，p17 的 MutationObserver 负责。
    window.addEventListener('resize', () => {
      if (panel && panel.offsetParent !== null) load();
    });
  }

  function fmt(s) { return s ? s.toFixed(1) + 's' : '—'; }

  function render() {
    if (!body) return;
    if (err) {
      body.innerHTML = `<div class="p18-placeholder">读不到耗时统计（${esc(err)}）。`
        + `后端未重启时 /api/perf 是 404，页面其余功能不受影响。</div>`;
      return;
    }
    if (!summary) { body.innerHTML = `<div class="p18-placeholder">还没有读数。</div>`; return; }
    const per = summary.agents || {};
    const ids = Object.keys(per);
    if (!ids.length) {
      body.innerHTML = `<div class="p18-placeholder">名册里没有人，算不出速度。</div>`;
      return;
    }
    const need = summary.min_samples || 20;
    const ranked = summary.ranked || [];
    let verdict;
    if (!ranked.length) {
      const most = Math.max(0, ...ids.map((a) => per[a].ms.n || 0));
      verdict = `<span class="p18-warn">样本不足：每人最多 ${most} ${MIN_HINT}，要看满 ${need} 条才排名。</span>`
        + `<div class="p18-note">拿 3 条算出来的「中位」就是那 3 条里最慢的一条，`
        + `这不是「谁慢」，是「谁刚好撞上一次」——所以这里不给排行。`
        + `下面标淡的数字是真的，只是条数太少。</div>`;
    } else if (ranked.length === 1) {
      // 只有一家的样本够。这时候写「最慢 X、最快 X」是句怪话。
      const only = ranked[0];
      verdict = `样本够排名的只有 <b>${esc(nameOf(only))}</b>（中位 ${fmt(per[only].ms.median)}）`
        + `<div class="p18-note">按全体 ${totalRows} 条算，不是最近 20 条。其余几家样本不足，不进榜。</div>`;
    } else {
      const worst = ranked[0];
      const best = ranked[ranked.length - 1];
      verdict = `最慢 <b>${esc(nameOf(worst))}</b>（中位 ${fmt(per[worst].ms.median)}）`
        + `，最快 ${esc(nameOf(best))}（${fmt(per[best].ms.median)}）`
        + `<div class="p18-note">按全体 ${totalRows} 条算，不是最近 20 条。</div>`;
    }
    let rows = '';
    for (const aid of ids) {
      const ms = per[aid].ms, tt = per[aid].ttft;
      // 样本不够也显示真数字，只是标淡 + 写明「别当结论」。
      // 原来这里换成一句「样本不足」把数抹掉，结果轻用的房间整栏都是同一句话，
      // 既看不出谁是刚跑过的那一家，也看不出差多少——0.0 更糟，看着像大家都很快。
      const dim = ms.insufficient;
      const cells = dim && !ms.n
        ? `<td class="dim" colspan="3">还没有记录</td>`
        : `<td class="num${dim ? ' dim' : ''}">${fmt(ms.median)}</td>`
          + `<td class="num${dim ? ' dim' : ''}">${fmt(ms.p90)}</td>`
          + `<td class="num${dim ? ' dim' : ''}">${dim ? '仅 ' + ms.n + ' 条' : ms.n}</td>`;
      const hint = dim ? (ms.n ? `只有 ${ms.n} 条样本，下面的数别当结论。` : '还没有可用的样本。') : '';
      rows += `<tr title="${esc(nameOf(aid))}：${hint}最长 ${fmt(ms.max)} · 首字中位 ${fmt(tt.median)}（${tt.n} 条）">`
        + `<td${dim ? ' class="dim"' : ''}>${esc(nameOf(aid))}</td>${cells}</tr>`;
    }
    body.innerHTML = `<div class="p18-verdict">${verdict}</div>
      <table class="p18-table">
        <colgroup><col class="c-who"><col><col><col></colgroup>
        <thead><tr><th>谁</th><th>中位</th><th>p90</th><th>样本</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="p18-note">带 <b>首字</b> 的读数只有 ${withTtft} 条（老记录没这个字段，`
        + `要跑几场才攒够）。悬停一行看它的最长和首字。</div>`;
  }

  function nameOf(aid) {
    const a = lastState && lastState.agents
      ? lastState.agents.find((x) => x.id === aid) : null;
    return (a && a.name) || aid;
  }

  let lastLoadAt = 0;
  async function load(minGapMs) {
    // 拖窗口会连着发几十个 resize，每次一个 fetch 没必要。面板可见时
    // 2 秒内不重复请求（点标题展开那条路径传 0，人要看的数得是新的）。
    const gap = minGapMs === 0 ? 0 : 2000;
    if (Date.now() - lastLoadAt < gap) return;
    lastLoadAt = Date.now();
    try {
      // refresh=1：把 turns.jsonl 也读进来。发言行不写审计，
      // 只读审计会系统性漏掉发言（实测 416 条只剩 53 条）。
      const r = await fetch('/api/perf?refresh=1', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      summary = data.summary || null;
      totalRows = data.total_rows || 0;
      withTtft = data.with_ttft || 0;
      err = null;
    } catch (e) {
      err = e && e.message ? e.message : String(e);
    }
    render();
  }

  /* ---- 底部条：本条还剩几秒 + 到点后「让它继续」 ---- */
  let budgetEl = null, resumeEl = null, budgetClosed = false, wasRunning = false;

  function ensureBar() {
    if (budgetEl) return true;
    const bar = document.querySelector('.bar');
    const status = document.getElementById('status');
    if (!bar || !status) return false;
    budgetEl = document.createElement('span');
    budgetEl.id = 'budget';
    budgetEl.title = '这一条消息的墙钟预算，用完就先停下（config.json: conversation.run_budget_seconds）';
    resumeEl = document.createElement('button');
    resumeEl.id = 'resume';
    resumeEl.textContent = '让它继续';
    resumeEl.title = '预算到点被停下了，点这里接着说（会重新给一整段预算）';
    resumeEl.addEventListener('click', async () => {
      resumeEl.disabled = true;
      try {
        const r = await fetch('/api/run', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ rounds: Number(document.getElementById('rounds')?.value || 1) }),
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        budgetClosed = false;
        resumeEl.classList.remove('show');
      } catch (e) {
        resumeEl.disabled = false;
        resumeEl.title = '启动失败：' + (e && e.message ? e.message : e);
      }
      return undefined;
    });
    bar.insertBefore(budgetEl, status);
    bar.insertBefore(resumeEl, status);
    return true;
  }

  function paintBudget(st) {
    if (!st) return;
    if (!ensureBar()) return;
    // 只在「上一场没跑、这一场在跑」这条边上作废旧的到点标记。
    // 用 st.running 单独判不行：播报「到点了」先于 running=false 的状态推送，
    // 那时候 running 还是 true，直接把标志抹掉，按钮就永远出不来。
    if (st.running && !wasRunning) budgetClosed = false;
    wasRunning = !!st.running;

    const left = st.budget_left_seconds;
    if (st.running && left !== null && left !== undefined) {
      budgetEl.textContent = `本条还剩 ${left}s`;
      budgetEl.className = left <= 15 ? 'tight' : '';
    } else {
      budgetEl.textContent = '';
      budgetEl.className = '';
    }
    resumeEl.classList.toggle('show', budgetClosed && !st.running);
    resumeEl.disabled = false;
  }

  // 房间自己会播报「这场到点了」。认这句话来亮「让它继续」，
  // 而不是加一个新字段——播报本来就要给人看，两处会不同步。
  function onNotice(m) {
    const text = (m && m.text) || '';
    if (/到点了/.test(text)) {
      budgetClosed = true;
      paintBudget(lastState);
    }
  }

  window.P18Latency = {
    refresh: load,
    onNotice,
    onState(st) {
      lastState = st;
      paintBudget(st);
      // 只在**看得见**的时候重算。/api/perf?refresh=1 要把 turns.jsonl 整个读一遍，
      // 每条状态推送都刷一次的话，面板自己就成了新的慢点。
      if (panel && panel.offsetParent !== null) load();
    },
  };

  mount();
  load();   // 起手一次：折叠状态下也要有数，展开时不至于先是一片空
})();
