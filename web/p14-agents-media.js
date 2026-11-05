// P14 — 页面内添加/删除 agent + 发图片
//
// 两个功能都不依赖 app.js 的内部变量，全部走自己的 REST 调用后调 pushState 之外的
// 轻量刷新（reconnect 由服务端处理，这里只更新侧栏 DOM）。
//
// 一个坑记在这：agent 列表原本由 app.js 从 /api/state 渲染，本文件改完 agent 后
// 必须让 app.js 重新渲染，否则会出现"配置里有了，侧栏没有"。做法是重新拉一次
// /api/state 再调用 window.__renderAgents（app.js 暴露的钩子，见下方 patch）。
(function () {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, txt) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  };

  // ---------------------------------------------------------------- 附件状态
  // 待发送的图片。发送成功后清空。
  const pending = [];

  function renderTray() {
    const tray = $('#p14-tray');
    if (!tray) return;
    tray.innerHTML = '';
    if (!pending.length) {
      tray.style.display = 'none';
      return;
    }
    tray.style.display = 'flex';
    pending.forEach((a, i) => {
      const box = el('div', 'p14-thumb');
      const img = el('img');
      img.src = a.url;
      img.alt = a.name || a.id;
      const x = el('button', 'p14-x', '×');
      x.title = '移除';
      x.onclick = () => { pending.splice(i, 1); renderTray(); };
      box.appendChild(img);
      box.appendChild(x);
      tray.appendChild(box);
    });
    const info = el('div', 'p14-info', pending.length + ' 张待发送');
    tray.appendChild(info);
  }

  async function uploadFile(file) {
    if (!file || !/^image\//.test(file.type)) {
      toast('只支持图片');
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      toast('图片超过 8MB');
      return;
    }
    const data = await new Promise((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.onerror = rej;
      fr.readAsDataURL(file);
    });
    try {
      const r = await fetch('/api/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        body: JSON.stringify({ data, name: file.name || 'paste.png' }),
      });
      const j = await r.json();
      if (!j.ok) { toast(j.error || '上传失败'); return; }
      // 去重：同一张图重复选不会出现两个缩略图
      if (!pending.some((p) => p.id === j.id)) pending.push(j);
      renderTray();
    } catch (e) {
      toast('上传失败：' + e.message);
    }
  }

  // ---------------------------------------------------------------- toast
  function toast(msg, bad) {
    let t = $('#p14-toast');
    if (!t) {
      t = el('div', 'p14-toast');
      t.id = 'p14-toast';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.className = 'p14-toast show' + (bad ? ' bad' : '');
    clearTimeout(t._h);
    t._h = setTimeout(() => { t.className = 'p14-toast'; }, 4000);
  }

  // ---------------------------------------------------------------- agent 管理
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  async function refreshAgents() {
    // 让 app.js 重新拉 /api/state 并重画侧栏。
    if (typeof window.__renderAgents === 'function') await window.__renderAgents();
  }

  function buildPanel() {
    const wrap = el('div', 'p14-panel');
    wrap.id = 'p14-panel';
    wrap.innerHTML = `
      <div class="p14-head" id="p14-toggle">
        <span>添加 agent</span><span class="p14-caret">▾</span>
      </div>
      <div class="p14-body" id="p14-body" hidden>
        <div class="p14-row">
          <select id="p14-preset"><option value="">— 选一个预设 —</option></select>
        </div>
        <div class="p14-row">
          <input id="p14-id" placeholder="id（小写字母/数字，如 kimi）" />
        </div>
        <div class="p14-row">
          <input id="p14-name" placeholder="显示名（如 Kimi Code）" />
        </div>
        <div class="p14-row">
          <input id="p14-cmd" placeholder="启动命令，如  kimi acp" />
        </div>
        <details class="p14-persona">
          <summary>人格（可选 · 不填就用通用默认）</summary>
          <div class="p14-row p14-color-row">
            <label for="p14-color">头像与名字的颜色</label>
            <input id="p14-color" type="color" value="#888888" />
            <span id="p14-color-val">#888888</span>
          </div>
          <div class="p14-row">
            <input id="p14-tag" placeholder="分工一句话 · 如「质疑前提 · 找没说出口的假设」" />
          </div>
          <div class="p14-row">
            <textarea id="p14-role" rows="4"
              placeholder="角色设定（第二人称，用它自己的口吻）。留空则按 id 取内置人格，没有就走通用兜底。"></textarea>
          </div>
          <div class="p14-row">
            <input id="p14-style" placeholder="说话风格一句话 · 如「先给结论再给理由，短句」" />
          </div>
        </details>
        <div class="p14-row p14-actions">
          <button id="p14-test" class="p14-btn">测试连接</button>
          <button id="p14-add" class="p14-btn primary">添加</button>
        </div>
        <div class="p14-log" id="p14-log"></div>
      </div>`;
    return wrap;
  }

  function logTo(msg, kind) {
    const box = $('#p14-log');
    if (!box) return;
    const line = el('div', 'p14-line' + (kind ? ' ' + kind : ''), msg);
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
  }

  // 给新 agent 挑一个还没被占用的身份色。
  // 撞色不是小事：房间靠"左侧色条 + 名字颜色"区分谁在说话，
  // 两个同色等于抹掉其中一个的身份。
  const PALETTE = [
    '#6ea8fe', '#ffb86c', '#d3a0ff', '#7ee787',
    '#56d4dd', '#ff7b72', '#f2cc60', '#79c0ff',
    '#b392f0', '#ffa657',
  ];

  // 颜色是不是"用户真的选过"。没动过的话，对已有 agent 不能发这个字段——
  // 一发改色就把人家原来的身份色覆盖了（配置是合并语义）。
  let colorTouched = false;
  let knownAgents = [];

  function setColor(c) {
    const inp = $('#p14-color');
    const lab = $('#p14-color-val');
    if (inp) inp.value = c;
    if (lab) lab.textContent = c;
  }

  async function loadKnownAgents() {
    try {
      const s = await (await fetch('/api/state', { cache: 'no-store' })).json();
      knownAgents = s.agents || [];
    } catch (e) {
      knownAgents = [];
    }
    return knownAgents;
  }

  function isExisting(id) {
    return knownAgents.some((a) => a.id === id);
  }

  async function pickFreeColor() {
    if (!knownAgents.length) await loadKnownAgents();
    const used = new Set(knownAgents.map((a) => String(a.color || '').toLowerCase()));
    return PALETTE.find((c) => !used.has(c.toLowerCase()))
      || PALETTE[used.length % PALETTE.length];
  }

  async function loadPresets() {
    try {
      const r = await fetch('/api/agent-presets');
      const list = await r.json();
      const sel = $('#p14-preset');
      list.forEach((p) => {
        const o = el('option', null, p.name + '  ·  ' + p.cmd.join(' '));
        o.value = p.id;
        sel.appendChild(o);
      });
      sel.onchange = () => {
        const p = list.find((x) => x.id === sel.value);
        if (!p) return;
        $('#p14-id').value = p.id;
        $('#p14-name').value = p.name;
        $('#p14-cmd').value = p.cmd.join(' ');
      };
    } catch (e) {
      /* 预设拿不到不影响手填 */
    }
  }

  function readForm() {
    const val = (id) => {
      const n = $('#' + id);
      return n ? n.value.trim() : '';
    };
    return {
      id: val('p14-id'),
      name: val('p14-name'),
      cmd: val('p14-cmd'),
      // 只有三种情况才发颜色：用户真的选了 / 这是新 agent（我们用配色板挑过）。
      // 已有 agent 且用户没碰过颜色 → 不发，配置里原来的身份色保持不变。
      color: (colorTouched || !isExisting(val('p14-id'))) ? val('p14-color') : '',
      tag: val('p14-tag'),
      role: val('p14-role'),
      style: val('p14-style'),
    };
  }

  async function probeCmd() {
    const f = readForm();
    if (!f.cmd) { toast('先填启动命令'); return; }
    logTo('探活中…（npx 首次可能要几十秒）');
    try {
      const r = await fetch('/api/agents/probe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        body: JSON.stringify({ cmd: f.cmd }),
      });
      const j = await r.json();
      if (j.ok) {
        const nm = (j.agentInfo && (j.agentInfo.title || j.agentInfo.name)) || '未知 agent';
        logTo(`✓ 连通：${nm} v${(j.agentInfo && j.agentInfo.version) || '?'}（${j.elapsed_ms}ms）`
          + (j.models ? '，支持切换模型' : '，无模型列表'), 'ok');
      } else {
        logTo('✗ ' + (j.error || '探活失败'), 'bad');
      }
    } catch (e) {
      logTo('✗ 请求失败：' + e.message, 'bad');
    }
  }

  async function addAgent() {
    const f = readForm();
    if (!f.id || !f.cmd) { toast('id 和命令都要填'); return; }
    $('#p14-add').disabled = true;
    logTo('添加中…（先探活再落地）');
    try {
      const r = await fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        body: JSON.stringify(f),
      });
      const j = await r.json();
      if (!j.ok) {
        logTo('✗ ' + (j.error || '添加失败'), 'bad');
        return;
      }
      const word = { added: '已接入', rebuilt: '已按新命令重建', unchanged: '已存在，未改动' }[j.action] || j.action;
      logTo(`✓ ${word}：${j.agent && j.agent.name}`, 'ok');
      await refreshAgents();
    } catch (e) {
      logTo('✗ 请求失败：' + e.message, 'bad');
    } finally {
      $('#p14-add').disabled = false;
    }
  }

  async function removeAgent(aid) {
    if (!confirm(`把 ${aid} 从房间移除？（已有发言会保留）`)) return;
    try {
      const r = await fetch('/api/agents/' + encodeURIComponent(aid), { method: 'DELETE' });
      const j = await r.json();
      if (!j.ok) { toast(j.error || '移除失败', true); return; }
      toast('已移除 ' + aid);
      await refreshAgents();
    } catch (e) {
      toast('移除失败：' + e.message, true);
    }
  }
  window.__p14RemoveAgent = removeAgent;

  // ---------------------------------------------------------------- 装配
  function mount() {
    const aside = document.querySelector('aside');
    if (!aside) return;

    // 1) agent 管理面板：放在侧栏 agent 列表下面
    const panel = buildPanel();
    const agentsBox = $('#agents');
    if (agentsBox && agentsBox.parentNode) {
      agentsBox.parentNode.insertBefore(panel, agentsBox.nextSibling);
    } else {
      aside.insertBefore(panel, aside.firstChild);
    }
    $('#p14-toggle').onclick = () => {
      const b = $('#p14-body');
      b.hidden = !b.hidden;
      $('#p14-panel').classList.toggle('open', !b.hidden);
      // 第一次展开时挑一个还没被占用的颜色，免得新人和别人撞色
      // （默认 #888888 会和任何灰色系头像混在一起，等于没有身份色）
      if (!b.hidden && !b.dataset.colorInit) {
        b.dataset.colorInit = '1';
        // 先拿到现有 agent 再挑色：既避免撞色，也让 isExisting() 有数据可用
        loadKnownAgents()
          .then(() => pickFreeColor())
          .then((c) => { if (c) setColor(c); });
      }
    };
    // 颜色选择器旁边实时显示色值，否则原生取色器关了之后看不出选了什么
    const colorInput = $('#p14-color');
    if (colorInput) {
      colorInput.addEventListener('input', () => {
        colorTouched = true;
        setColor(colorInput.value);
      });
    }
    $('#p14-test').onclick = probeCmd;
    $('#p14-add').onclick = addAgent;
    loadPresets();

    // 2) 图片：缩略图条放在输入框上方
    const row = document.querySelector('footer .row');
    if (row && row.parentNode) {
      const tray = el('div', 'p14-tray');
      tray.id = 'p14-tray';
      row.parentNode.insertBefore(tray, row);
    }

    // 3) 图片：粘贴 + 选文件 + 拖拽
    const input = $('#input');
    if (input) {
      input.addEventListener('paste', (e) => {
        const items = (e.clipboardData && e.clipboardData.items) || [];
        for (const it of items) {
          if (it.kind === 'file' && /^image\//.test(it.type)) {
            e.preventDefault();
            uploadFile(it.getAsFile());
          }
        }
      });
      // 拖拽把文件丢进输入框
      input.addEventListener('dragover', (e) => { e.preventDefault(); input.classList.add('p14-drop'); });
      input.addEventListener('dragleave', () => input.classList.remove('p14-drop'));
      input.addEventListener('drop', (e) => {
        e.preventDefault();
        input.classList.remove('p14-drop');
        for (const f of e.dataTransfer.files || []) uploadFile(f);
      });
    }

    // 4) 图片按钮：插在发送按钮左边
    const sendBtn = $('#send');
    if (sendBtn && sendBtn.parentNode) {
      const pick = el('button', 'p14-pick', '🖼');
      pick.title = '发送图片（也可以直接粘贴或拖进来）';
      const file = el('input');
      file.type = 'file';
      file.accept = 'image/*';
      file.multiple = true;
      file.style.display = 'none';
      pick.onclick = () => file.click();
      file.onchange = () => {
        for (const f of file.files || []) uploadFile(f);
        file.value = '';
      };
      sendBtn.parentNode.insertBefore(pick, sendBtn);
      sendBtn.parentNode.insertBefore(file, sendBtn);
    }

    // 5) 拦截发送：带上附件
    //    app.js 的 send() 是全局函数，这里包一层：先发正文+图片，再放开原逻辑。
    const origSend = window.send;
    if (typeof origSend === 'function') {
      window.send = async function () {
        const text = ($('#input').value || '').trim();
        if (pending.length && !text) {
          toast('有图片时请加一句说明');
          return;
        }
        if (!pending.length) return origSend.apply(this, arguments);

        const imgs = pending.map((p) => ({ id: p.id, name: p.name }));
        const rounds = Number(($('#rounds') || {}).value || 1);
        const auto = !!($('#auto') || {}).checked;
        try {
          await fetch('/api/say', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify({ text, auto, rounds, images: imgs }),
          });
          $('#input').value = '';
          $('#input').style.height = 'auto';
          pending.length = 0;
          renderTray();
        } catch (e) {
          toast('发送失败：' + e.message, true);
        }
      };
    }

    // 6) 移除按钮：靠事件委托挂在 agent 卡片上
    if (agentsBox) {
      agentsBox.addEventListener('click', (e) => {
        const b = e.target.closest('.p14-rm');
        if (b) removeAgent(b.dataset.aid);
      });
    }
  }

  // ---------------------------------------------------------------- 样式
  const css = `
  /* minmax(0,1fr) 是必需的：grid 子项默认 min-width:auto，会被 <select> 的
     最长 option 文本（"Kimi Code · kimi ac ..."）撑到 361px，在 240px 的侧栏里溢出 122px。
     这是个只会在窄容器里暴露的 bug —— 宽屏看不出来。 */
  .p14-panel{margin:8px 10px;border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden;
    font-size:var(--fs-sm);min-width:0}
  .p14-head{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;
    cursor:pointer;user-select:none;color:var(--dim);transition:color .12s,background .12s}
  .p14-head:hover{background:var(--panel2);color:var(--text)}
  .p14-panel.open .p14-caret{transform:rotate(180deg)}
  .p14-caret{transition:transform .15s;font-size:10px;opacity:.7}
  .p14-body{padding:0 12px 12px;display:grid;gap:var(--sp-2);grid-template-columns:minmax(0,1fr)}
  .p14-row{display:flex;gap:var(--sp-2);min-width:0}
  .p14-row input,.p14-row select{flex:1 1 0;width:100%;min-width:0;max-width:100%;
    background:var(--bg);border:1px solid var(--line);
    color:inherit;border-radius:var(--r-sm);padding:7px 9px;font-size:var(--fs-sm);font-family:inherit}
  .p14-row select{text-overflow:ellipsis}
  /* 人格是可选块：默认收起，不占侧栏高度；展开后是四个字段 */
  .p14-persona{border:1px solid var(--line-soft);border-radius:var(--r-sm);padding:0}
  .p14-persona>summary{cursor:pointer;user-select:none;list-style:none;padding:7px 9px;
    font-size:var(--fs-xs);color:var(--dim);border-radius:var(--r-sm)}
  .p14-persona>summary::-webkit-details-marker{display:none}
  .p14-persona>summary:hover{color:var(--text);background:var(--panel2)}
  .p14-persona[open]>summary{border-bottom:1px solid var(--line-soft);border-radius:var(--r-sm) var(--r-sm) 0 0}
  .p14-persona>.p14-row{margin:8px 9px}
  .p14-persona>.p14-row:last-child{margin-bottom:10px}
  .p14-row textarea{flex:1 1 0;width:100%;min-width:0;max-width:100%;resize:vertical;
    background:var(--bg);border:1px solid var(--line);color:inherit;border-radius:var(--r-sm);
    padding:7px 9px;font-size:var(--fs-sm);font-family:inherit;line-height:1.5}
  .p14-color-row{align-items:center;gap:var(--sp-2)}
  .p14-color-row label{font-size:var(--fs-xs);color:var(--dim);flex:1 1 auto;min-width:0}
  .p14-color-row input[type=color]{flex:0 0 34px;width:34px;height:26px;padding:2px;cursor:pointer}
  #p14-color-val{font-family:Consolas,"Cascadia Mono",monospace;font-size:var(--fs-xs);
    color:var(--dim2);flex:0 0 auto;min-width:58px;text-align:right}
  .p14-actions{justify-content:flex-end}
  .p14-btn{background:var(--panel2);border:1px solid var(--line);color:inherit;border-radius:var(--r-sm);
    padding:6px 12px;cursor:pointer;font-size:var(--fs-sm)}
  .p14-btn.primary{background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:600}
  .p14-btn:disabled{opacity:.5;cursor:default}
  .p14-log{max-height:140px;overflow:auto;font-family:Consolas,"Cascadia Mono",monospace;
    font-size:var(--fs-xs);line-height:1.55;background:#00000045;border-radius:var(--r-sm);
    padding:8px 10px;display:none;white-space:pre-wrap;word-break:break-all}
  .p14-log:not(:empty){display:block}
  .p14-line.ok{color:var(--ok,#3fb950)}
  .p14-line.bad{color:var(--err,#f85149)}
  .p14-tray{display:none;gap:var(--sp-2);align-items:center;padding:var(--sp-3) var(--sp-4) 0;flex-wrap:wrap}
  .p14-thumb{position:relative;width:60px;height:60px;border-radius:var(--r-sm);overflow:hidden;border:1px solid var(--line)}
  .p14-thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .p14-x{position:absolute;top:1px;right:1px;width:16px;height:16px;line-height:14px;text-align:center;
    border-radius:50%;border:none;background:rgba(0,0,0,.65);color:#fff;cursor:pointer;font-size:12px;padding:0}
  .p14-info{font-size:var(--fs-xs);color:var(--dim2)}
  .p14-pick{background:transparent;border:1px solid var(--line);border-radius:8px;cursor:pointer;
    font-size:15px;padding:0 10px;color:inherit}
  .p14-pick:hover{background:rgba(255,255,255,.06)}
  #input.p14-drop{outline:2px dashed var(--ok,#3fb950);outline-offset:2px}
  .p14-toast{position:fixed;left:50%;bottom:88px;transform:translateX(-50%) translateY(20px);opacity:0;
    background:#222;color:#fff;padding:9px 16px;border-radius:8px;font-size:12px;pointer-events:none;
    transition:opacity .18s,transform .18s;z-index:9999;max-width:70vw}
  .p14-toast.show{opacity:.96;transform:translateX(-50%) translateY(0)}
  .p14-toast.bad{background:#7d2020}
  .p14-rm{position:absolute;top:4px;right:6px;background:transparent;border:none;color:var(--err,#f85149);
    cursor:pointer;font-size:13px;padding:0 2px;opacity:0;transition:opacity .12s}
  #agents > * {position:relative}
  #agents > *:hover .p14-rm{opacity:.9}
  `;

  function injectCss() {
    if (document.getElementById('p14-css')) return;
    const st = el('style');
    st.id = 'p14-css';
    st.textContent = css;
    document.head.appendChild(st);
  }

  function boot() {
    injectCss();
    mount();
    // 给每个 agent 卡片加删除按钮（app.js 重画后也要补，所以挂观察器）
    const decorate = () => {
      document.querySelectorAll('#agents [data-agent-id]').forEach((card) => {
        if (card.querySelector('.p14-rm')) return;
        const b = el('button', 'p14-rm', '×');
        b.dataset.aid = card.dataset.agentId;
        b.title = '移除这个 agent';
        card.appendChild(b);
      });
    };
    decorate();
    const box = $('#agents');
    if (box) new MutationObserver(decorate).observe(box, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
