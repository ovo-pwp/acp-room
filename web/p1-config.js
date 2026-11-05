/* p1-config.js —— P1 模型与配置选择器（claude）
 *
 * 挂在侧栏 #agents 之后插入独立面板。挂在 #agents 里面会被 app.js 的 renderAgents()
 * 每次重绘抹掉，所以面板独立、自己刷新。
 *
 * 数据流：
 *   GET  /api/options  → {agentId: {model, modelCount, options:[{id,name,category,currentValue,options:[...]}]}}
 *   POST /api/options  → {agent, configId, value}，返回 {ok, entry:{currentValue, verified}}
 *
 * 982 个模型不能全塞 <select>：model 用可搜索输入框 + 过滤列表（最多渲染 60 条匹配，
 * 键盘 ↑↓ 选择、回车选中第一条、Esc 关闭）；其它配置项（mode/effort/thinking…）
 * 选项少，用 <select> 收进折叠面板。
 * 切换后立即用 POST 返回值更新界面，不等下一轮。
 *
 * 合并方式（pi 操作）：index.html 里加一行 <script src="/static/p1-config.js"></script>
 * 即可。本文件不依赖 app.js 内部变量，只依赖 /api/options 两个端点。
 */
(function () {
  'use strict';

  const API = '/api/options';
  const MAX_ROWS = 60;

  // ---- P16：模型真假标记（codex 实测 10 个里 9 个是 404 空壳）----
  // 数据源两级：localStorage 缓存（先读，不前台探测）+ /api/models/probe（P14 批量巡检，
  // 点"检测全部"才调，probing:true 时轮询）；P5 的切换时 reachable 也回填缓存。
  const PROBE_NS = 'p1-probe-';
  const probe = {};      // aid -> {reachable:[], dead:[], checked, took_ms, ts}
  const showDead = {};   // aid -> bool（默认隐藏 dead）

  function probeLoad(aid) {
    try {
      const v = JSON.parse(localStorage.getItem(PROBE_NS + aid) || 'null');
      if (v && Array.isArray(v.reachable)) probe[aid] = v;
    } catch { /* ignore */ }
  }
  function probeSave(aid) {
    try { localStorage.setItem(PROBE_NS + aid, JSON.stringify(probe[aid])); } catch { /* ignore */ }
  }
  function probeStatus(aid, value) {   // 'ok' | 'dead' | null（未检测）
    const p = probe[aid];
    if (!p) return null;
    if ((p.reachable || []).includes(value)) return 'ok';
    if ((p.dead || []).includes(value)) return 'dead';
    return null;
  }
  function updateWarn(aid) {           // 当前模型是 dead → 红色警告条
    if (!section) return;
    const block = [...section.querySelectorAll('.p1-agent')].find(x => x.dataset.aid === aid);
    if (!block) return;
    const w = block.querySelector('.p1-warn');
    if (!w) return;
    const snap = opts && opts[aid];
    const me = snap && (snap.options || []).find(o => o.category === 'model');
    const cur = me ? me.currentValue : null;
    if (cur && probeStatus(aid, cur) === 'dead') {
      w.style.display = '';
      w.innerHTML = '⚠ 当前模型 <b>' + esc(cur) + '</b> 不可用——选了它不会有回答';
    } else {
      w.style.display = 'none';
    }
  }

  let opts = null;      // GET /api/options 快照
  let section = null;   // 本面板容器

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  }

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  async function refresh() {
    try {
      const r = await fetch(API);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      opts = await r.json();
      render();
    } catch (e) {
      if (section) {
        section.querySelector('.p1-list').innerHTML =
          '<div class="p1-err">/api/options 不可用：' + esc(e.message) + '（后端没起？）</div>';
      }
    }
  }

  function render() {
    if (!section || !opts) return;
    const list = section.querySelector('.p1-list');
    list.innerHTML = '';
    for (const [aid, snap] of Object.entries(opts || {})) {
      list.appendChild(agentBlock(aid, snap));
    }
  }

  function agentBlock(aid, snap) {
    const items = snap.options || [];
    const modelEntry = items.find(o => o.category === 'model') || null;
    const others = items.filter(o => o.category !== 'model' && (o.options || []).length > 0);
    const n = modelEntry ? (modelEntry.options || []).length : (snap.modelCount || 0);
    let cur = modelEntry ? modelEntry.currentValue : (snap.model || '?');

    const block = el('div', 'p1-agent');
    block.dataset.aid = aid;
    probeLoad(aid);   // P16：先读缓存，不前台探测
    const head = el('button', 'p1-head');
    head.innerHTML = '<span class="p1-arrow">▸</span><b>' + esc(aid) + '</b>' +
      '<span class="p1-cur">' + esc(cur) + '</span>' +
      '<span class="p1-count">' + n + ' 个模型</span>';
    const body = el('div', 'p1-body');
    body.style.display = 'none';
    const warn = el('div', 'p1-warn', '');
    warn.style.display = 'none';
    body.appendChild(warn);

    if (modelEntry && (modelEntry.options || []).length) {
      body.appendChild(modelPicker(aid, modelEntry, () => cur, v => { cur = v; }));
    } else {
      body.appendChild(el('div', 'p1-note', '该 agent 没有可切换的模型'));
    }

    if (others.length) {
      const det = el('details', 'p1-extra');
      det.innerHTML = '<summary>其它配置项（' + others.length + '）</summary>';
      const box = el('div', 'p1-extra-box');
      others.forEach(o => box.appendChild(optionSelect(aid, o)));
      det.appendChild(box);
      body.appendChild(det);
    }

    head.addEventListener('click', () => {
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : '';
      head.querySelector('.p1-arrow').textContent = open ? '▸' : '▾';
      if (!open) updateWarn(aid);   // P16：展开时刷新 dead 警告
    });
    block.appendChild(head);
    block.appendChild(body);
    updateWarn(aid);
    return block;
  }

  function modelPicker(aid, entry, getCur, setCur) {
    const box = el('div', 'p1-pick');
    const input = el('input');
    input.type = 'text';
    input.placeholder = '搜索 ' + (entry.options || []).length + ' 个模型…';
    const info = el('div', 'p1-info', '');
    const dd = el('div', 'p1-dd');
    let sel = -1, shown = [];

    function matches(q) {
      q = q.trim().toLowerCase();
      const src = entry.options || [];
      if (!q) return src.slice(0, MAX_ROWS);
      const all = [];
      for (let i = 0; i < src.length && all.length < MAX_ROWS; i++) {
        const o = src[i];
        if ((o.value + ' ' + (o.name || '')).toLowerCase().includes(q)) all.push(o);
      }
      return all;
    }

    function redraw() {
      const q = input.value.trim().toLowerCase();
      const src = matches(q);
      const showAll = !!showDead[aid];
      const rows = [];
      for (const o of src) {
        const st = probeStatus(aid, o.value);   // P16：真假标记
        const isCur = o.value === getCur();
        // 默认隐藏 dead；搜索关键词或当前模型除外（当前模型必须永远可见可切走）
        if (st === 'dead' && !showAll && !q && !isCur) continue;
        rows.push(o);
      }
      shown = rows;
      sel = rows.length ? 0 : -1;
      dd.innerHTML = '';
      rows.forEach((o, k) => {
        const st = probeStatus(aid, o.value);
        const isCur = o.value === getCur();
        const mark = st === 'ok' ? '<span class="p1-mark ok">✓</span>'
                   : st === 'dead' ? '<span class="p1-mark dead">✗</span>'
                   : '<span class="p1-mark unk">?</span>';
        const deadTxt = st === 'dead' ? '<span class="p1-deadtxt">不可用</span>' : '';
        const row = el('div', 'p1-row' + (k === sel ? ' on' : '') +
          (isCur ? ' cur' : ''));
        row.dataset.value = o.value;
        row.innerHTML = mark + '<span class="p1-val">' + esc(o.value) + '</span>' +
          '<span class="p1-nm">' + esc(o.name !== o.value ? o.name : '') + '</span>' +
          deadTxt;
        row.addEventListener('mousedown', ev => { ev.preventDefault(); pick(o.value); });
        dd.appendChild(row);
      });
      const p = probe[aid];
      const probeInfo = p
        ? ' · 已检测 ' + ((p.reachable || []).length + (p.dead || []).length) +
          ' 个（✓ ' + (p.reachable || []).length + ' ✗ ' + (p.dead || []).length + '）'
        : ' · 未检测';
      const hidden = (entry.options || []).length - rows.length;
      if (q) info.textContent = '匹配 ' + shown.length + ' 条（显示前 ' + MAX_ROWS +
        '）· 回车选第一条 · ↑↓ 移动' + probeInfo;
      else info.textContent = '共 ' + (entry.options || []).length +
        ' 个模型 · 显示前 ' + MAX_ROWS + '，输入关键词过滤' + probeInfo +
        (hidden > 0 ? ' · 已隐藏 ' + hidden + ' 个不可用（勾"显示不可用"放出）' : '');
      info.style.color = shown.length ? '' : 'var(--err)';
    }

    async function pick(v) {
      input.value = v;
      input.disabled = true;
      info.textContent = '切换中…';
      info.style.color = '';
      let j = {};
      try {
        const r = await fetch(API, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({agent: aid, configId: entry.id, value: v})
        });
        j = await r.json().catch(() => ({}));
      } catch (e) {
        info.textContent = '切换失败：' + e.message;
        info.style.color = 'var(--err)';
        input.disabled = false;
        return;
      }
      input.disabled = false;
      if (!j.ok || !j.entry) {
        info.textContent = '切换失败：' + (j.error || 'HTTP ' + r.status);
        info.style.color = 'var(--err)';
        return;
      }
      // 立即反映，不等下一轮：用返回的 currentValue 更新界面与本地快照
      const actual = j.entry.currentValue;
      setCur(actual);
      if (opts && opts[aid]) {
        const e2 = (opts[aid].options || []).find(o => o.id === entry.id);
        if (e2) e2.currentValue = actual;
        if (entry.category === 'model') opts[aid].model = actual;
      }
      // P16：P5 切换时探测的结果回填缓存（探测接口没就绪也不影响切换本身）
      if (j.entry && typeof j.entry.reachable === 'boolean') {
        const p = probe[aid] || { reachable: [], dead: [] };
        p.reachable = (p.reachable || []).filter(x => x !== actual);
        p.dead = (p.dead || []).filter(x => x !== actual);
        (j.entry.reachable ? p.reachable : p.dead).push(actual);
        p.ts = Date.now();
        probe[aid] = p;
        probeSave(aid);
      }
      updateWarn(aid);
      blockCur(aid, actual);
      dd.innerHTML = '';
      redraw();
      // redraw() 会把 info 重写成搜索帮助行——成功提示放在它后面，别被盖掉
      info.textContent = '已切到 ' + actual +
        (j.entry.verified ? '（后端已确认）' : '（后端返回值与目标不一致！）');
      info.style.color = j.entry.verified ? 'var(--ok)' : 'var(--err)';
    }

    function close() { dd.innerHTML = ''; }
    function redrawSel() {
      [...dd.children].forEach((r, k) => r.classList.toggle('on', k === sel));
    }
    input.addEventListener('input', redraw);
    input.addEventListener('focus', redraw);
    input.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') { ev.preventDefault(); if (shown[sel]) pick(shown[sel].value); }
      else if (ev.key === 'ArrowDown' && shown.length) { sel = Math.min(sel + 1, shown.length - 1); redrawSel(); }
      else if (ev.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); redrawSel(); }
      else if (ev.key === 'Escape') close();
    });
    document.addEventListener('click', ev => { if (!box.contains(ev.target)) close(); });

    // ---- P16：批量检测（/api/models/probe，P14 接；没就绪只提示不阻塞）----
    const bar = el('div', 'p1-probe-bar');
    bar.innerHTML = '<button class="p1-probe-btn">检测全部</button>' +
      '<label class="p1-showdead"><input type="checkbox"> 显示不可用</label>' +
      '<span class="p1-probe-st"></span>';
    const probeBtn = bar.querySelector('.p1-probe-btn');
    const probeChk = bar.querySelector('input');
    const probeSt = bar.querySelector('.p1-probe-st');
    probeChk.checked = !!showDead[aid];
    probeChk.addEventListener('change', () => { showDead[aid] = probeChk.checked; redraw(); });
    probeBtn.addEventListener('click', async () => {
      probeBtn.disabled = true;
      probeSt.textContent = '检测中…';
      probeSt.style.color = '';
      try {
        let j = await probeFetch(aid);
        while (j && j.probing) {   // 后端在跑批量巡检 → 轮询进度
          probeSt.textContent = '检测中（已查 ' + (j.checked || '?') + '）…';
          await new Promise(res => setTimeout(res, 2000));
          j = await probeFetch(aid);
        }
        if (j && Array.isArray(j.reachable)) {
          probe[aid] = { reachable: j.reachable || [], dead: j.dead || [],
                         checked: j.checked, took_ms: j.took_ms, ts: Date.now() };
          probeSave(aid);
          probeSt.textContent = '✓ ' + (j.reachable || []).length + ' 可用 · ✗ ' +
            (j.dead || []).length + ' 不可用 · ' + (j.took_ms || 0) + 'ms';
        } else {
          probeSt.textContent = '接口返回异常（没有 reachable 数组）';
        }
      } catch (e) {
        probeSt.textContent = '检测接口未就绪：' + e.message;
        probeSt.style.color = 'var(--warn)';
      }
      probeBtn.disabled = false;
      updateWarn(aid);
      redraw();
    });
    async function probeFetch(aid2) {
      const r = await fetch('/api/models/probe?agent=' + encodeURIComponent(aid2));
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }

    box.appendChild(input);
    box.appendChild(info);
    box.appendChild(bar);
    box.appendChild(dd);
    return box;
  }

  function blockCur(aid, v) {
    if (!section) return;
    const b = [...section.querySelectorAll('.p1-agent')].find(x => x.dataset.aid === aid);
    if (b) b.querySelector('.p1-cur').textContent = v;
  }

  function optionSelect(aid, o) {
    const wrap = el('div', 'p1-opt');
    wrap.innerHTML = '<div class="p1-opt-name">' + esc(o.name || o.id) + '</div>';
    const sel = el('select');
    (o.options || []).forEach(opt => {
      const op = document.createElement('option');
      op.value = opt.value;
      op.textContent = opt.value;
      if (opt.value === o.currentValue) op.selected = true;
      sel.appendChild(op);
    });
    const st = el('span', 'p1-opt-st', '');
    sel.addEventListener('change', async () => {
      sel.disabled = true;
      let j = {};
      try {
        const r = await fetch(API, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({agent: aid, configId: o.id, value: sel.value})
        });
        j = await r.json().catch(() => ({}));
      } catch (e) {
        st.textContent = '失败：' + e.message;
        st.style.color = 'var(--err)';
        sel.disabled = false;
        return;
      }
      sel.disabled = false;
      if (j.ok && j.entry) {
        o.currentValue = j.entry.currentValue;
        st.textContent = '→ ' + j.entry.currentValue + (j.entry.verified ? ' ✓' : '（未确认）');
        st.style.color = j.entry.verified ? 'var(--ok)' : 'var(--err)';
      } else {
        st.textContent = '失败：' + (j.error || 'HTTP ' + r.status);
        st.style.color = 'var(--err)';
      }
    });
    wrap.appendChild(sel);
    wrap.appendChild(st);
    return wrap;
  }

  function injectStyle() {
    if (document.getElementById('p1-style')) return;
    const st = document.createElement('style');
    st.id = 'p1-style';
    st.textContent = `
.p1-config { border-top: 1px solid var(--line); padding: 10px; display: flex;
  flex-direction: column; gap: 6px; max-height: 42vh; overflow-y: auto; }
.p1-title { font-size: 12px; color: var(--dim); display: flex; justify-content: space-between;
  align-items: center; font-weight: 600; letter-spacing: .3px; }
.p1-refresh { background: none; border: 1px solid var(--line); color: var(--dim);
  border-radius: 6px; cursor: pointer; padding: 0 7px; font-size: 12px; }
.p1-refresh:hover { color: var(--accent); border-color: var(--accent); }
.p1-agent { border: 1px solid var(--line); border-radius: 8px; background: var(--panel2); overflow: hidden; }
.p1-head { width: 100%; display: flex; gap: 6px; align-items: center; background: none;
  border: none; color: var(--text); padding: 7px 9px; cursor: pointer; font: inherit; text-align: left; }
.p1-head:hover { color: var(--accent); }
.p1-arrow { color: var(--dim); width: 10px; flex: none; }
.p1-head b { font-size: 12px; }
.p1-cur { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 11px; color: var(--ok); font-family: Consolas, "Cascadia Mono", monospace; }
.p1-count { font-size: 10px; color: var(--dim); flex: none; }
.p1-body { padding: 0 9px 9px; display: flex; flex-direction: column; gap: 8px; }
.p1-pick input { width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 8px; font: inherit; font-size: 12px; outline: none; }
.p1-pick input:focus { border-color: var(--accent); }
.p1-info { font-size: 10px; color: var(--dim); margin-top: 4px; }
.p1-dd { border: 1px solid var(--line); border-radius: 6px; max-height: 180px; overflow-y: auto;
  background: var(--bg); margin-top: 4px; }
.p1-row { padding: 4px 8px; font-size: 11px; cursor: pointer; display: flex; gap: 6px; align-items: baseline; }
.p1-row.on { background: var(--panel2); }
.p1-row:hover { background: var(--panel2); }
.p1-row.cur .p1-val { color: var(--ok); }
.p1-val { font-family: Consolas, "Cascadia Mono", monospace; color: var(--accent); }
.p1-nm { color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p1-extra { border-top: 1px solid var(--line); padding-top: 6px; }
.p1-extra summary { cursor: pointer; font-size: 11px; color: var(--dim); }
.p1-extra-box { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.p1-opt { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.p1-opt-name { font-size: 11px; color: var(--dim); min-width: 60px; }
.p1-opt select { background: var(--bg); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 4px 6px; font: inherit; font-size: 12px; flex: 1; min-width: 0; }
.p1-opt-st { font-size: 10px; color: var(--dim); }
.p1-note { font-size: 11px; color: var(--dim); }
.p1-err { font-size: 11px; color: var(--err); padding: 4px 0; }
/* P16：模型真假标记 */
.p1-mark { width: 14px; text-align: center; flex: none; font-size: 11px; font-weight: 700; }
.p1-mark.ok { color: var(--ok); }
.p1-mark.dead { color: var(--err); }
.p1-mark.unk { color: var(--dim); }
.p1-deadtxt { color: var(--err); font-size: 10px; flex: none; }
.p1-warn { border: 1px solid var(--err); background: #ff7b7214; color: var(--err);
  border-radius: 6px; padding: 5px 8px; font-size: 11px; margin-bottom: 2px; }
.p1-warn b { font-family: Consolas, "Cascadia Mono", monospace; }
.p1-probe-bar { display: flex; gap: 8px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
.p1-probe-btn { background: none; border: 1px solid var(--line); color: var(--dim);
  border-radius: 6px; cursor: pointer; padding: 2px 8px; font-size: 11px; }
.p1-probe-btn:hover { color: var(--accent); border-color: var(--accent); }
.p1-probe-btn:disabled { opacity: .5; cursor: wait; }
.p1-showdead { display: flex; gap: 4px; align-items: center; color: var(--dim); font-size: 11px; }
.p1-probe-st { color: var(--dim); font-size: 10px; }
`;
    document.head.appendChild(st);
  }

  function mount() {
    const agentsEl = document.getElementById('agents');
    if (!agentsEl || !agentsEl.parentElement) return;
    section = el('section', 'p1-config');
    section.innerHTML = '<div class="p1-title">模型与配置' +
      '<button class="p1-refresh" title="重新拉取">↻</button></div>' +
      '<div class="p1-list"></div>';
    agentsEl.parentElement.insertBefore(section, agentsEl.nextElementSibling);
    section.querySelector('.p1-refresh').addEventListener('click', refresh);
    injectStyle();
  }

  function boot() {
    mount();
    refresh();
    setInterval(refresh, 30000);   // 别的窗口/进程改过配置也能看到
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // G4：侧栏点模型名 → 直接展开这个 agent 的卡片并把光标放进搜索框。
  // 否则换一次模型要点四下（开抽屉 → 配置标签 → 卡片 → 输入框），
  // 而这是这个页面上最高频的一个操作。
  function openFor(aid) {
    if (!section) return false;
    const block = section.querySelector('.p1-agent[data-aid="' + aid + '"]');
    if (!block) { refresh(); return false; }   // 快照还没到，先补拉一次
    const head = block.querySelector('.p1-head');
    const body = block.querySelector('.p1-body');
    if (head && body && body.style.display === 'none') head.click();
    const input = block.querySelector('.p1-pick input');
    if (input) {
      // 抽屉刚展开时宽度还是 0，focus 要等一帧
      setTimeout(() => { input.focus(); input.select(); }, 0);
    }
    block.scrollIntoView({block: 'nearest'});
    return true;
  }

  window.P1Config = { refresh, openFor };
})();
