const logEl = document.getElementById('log');
const agentsEl = document.getElementById('agents');
const inputEl = document.getElementById('input');
const sendEl = document.getElementById('send');
const stopEl = document.getElementById('stop');
const statusEl = document.getElementById('status');

let state = null;
let live = {};        // agent -> {name, color, text, tools:[]}
let ws = null;
let order = [];

function esc(s) {
  // 先 String()：数字/0/false 直接 .replace 会 TypeError，而 `s || ''` 还会把 0
  // 静默变成空串（更隐蔽）。P9 的 turn_seq（数字）就踩到了这个坑。
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

// 极简 markdown：围栏代码块 + 行内代码 + 粗体。不做完整解析器，够用即可。
function md(s) {
  const parts = [];
  let rest = s || '';
  const fence = /```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```/g;
  let last = 0, m;
  while ((m = fence.exec(rest))) {
    parts.push({t: 'text', v: rest.slice(last, m.index)});
    parts.push({t: 'code', v: m[2]});
    last = m.index + m[0].length;
  }
  parts.push({t: 'text', v: rest.slice(last)});
  return parts.map(p => {
    if (p.t === 'code') return '<pre>' + esc(p.v) + '</pre>';
    return esc(p.v)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  }).join('');
}

function initials(name) {
  const n = (name || '?').trim();
  return n.length <= 2 ? n : n.slice(0, 2);
}

// 颜色来自 config.json，会被直接插进 style 属性。只放行纯色值：
// 一个带引号的字符串能从这个属性里逃出去，拼出任意标签。
function safeColor(c, fallback = '#888888') {
  const s = String(c || '').trim();
  if (/^#[0-9a-f]{3,8}$/i.test(s)) return s;
  if (/^rgba?\(\s*[\d.,\s%]+\)$/i.test(s)) return s;
  return fallback;
}

function renderAgents() {
  if (!state) return;
  agentsEl.innerHTML = '';
  const byId = Object.fromEntries(state.agents.map(a => [a.id, a]));
  order.forEach((id, i) => {
    const a = byId[id];
    if (!a) return;
    const conn = a.connection || '未连接';
    const ok = conn === '在线' || conn === '发言中';
    const color = ok ? (conn === '发言中' ? 'var(--warn)' : 'var(--ok)') : 'var(--err)';
    // G2：agent 有没有接回上次的会话。"new" 意味着它其实不记得之前聊过什么，
    // 这一眼能区分"它装糊涂"和"它真记得"。
    const resumed = a.session_origin === 'resume' || a.session_origin === 'load';
    // G5：存过会话却没接回来——必须说出来，否则用户看到的是"它突然变蠢"。
    // 原因挂在 title 上（codex 常见的是那次会话当时还没落盘，没什么可接）。
    const lost = (!resumed && a.resume_error)
      ? `<span class="rs lost" title="${esc(a.resume_error)}">上次会话没接回</span>`
      : '';
    // G4：模型显示"当前真正生效的那个"（state.opts 里的 brief 值），不是 config.json
    // 写的静态基线——手点切换、启动预置、agent 自己的默认都只体现在前者上。
    // 没发现过配置时退回 model_hint，至少这一行不会空着。
    const live = (state.opts && state.opts[a.id] && state.opts[a.id].model) || '';
    const model = live || (a.config && a.config.model) || '';
    const chip = model
      ? `<button class="mchip" title="点这里换模型（当前 ${esc(model)}）">${esc(model)}</button>`
      : '';
    const hint = chip || esc(a.model_hint || '');
    const el = document.createElement('div');
    el.className = 'agent';
    el.dataset.agentId = a.id;      // P14：删除按钮按这个 id 找人
    el.title = '点击切换是否参与本轮发言';
    const ac = safeColor(a.color);
    el.style.setProperty('--c', ac);   // 名字颜色 / 高亮 / 光晕都从这里取
    el.innerHTML = `
      <div class="dot" style="background:${color};color:${color}"></div>
      <div class="meta">
        <div class="nm">${esc(a.name)}</div>
        <div class="hint">${hint}${resumed ? '<span class="rs">接回上次会话</span>' : ''}${lost}</div>
        <div class="st" style="color:${color}">${esc(conn)}</div>
      </div>
      <div class="idx">${i + 1}</div>`;
    const btn = el.querySelector('.mchip');
    if (btn) {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();      // 整行是「是否参与本轮」的开关，别一起触发
        openModelPicker(a.id);
      });
    }
    agentsEl.appendChild(el);
  });
}

// G4：模型选择器本体在 p1-config.js（可搜索的列表），抽屉外壳在 p17-shell.js。
// 这里只负责「把人送到那一个 agent 的那一个输入框前」，省掉开抽屉→找标签→找卡片三次点击。
function openModelPicker(aid) {
  if (window.P17Shell) window.P17Shell.openPanel('cfg');
  if (window.P1Config) window.P1Config.openFor(aid);
}

// P14 接缝：增删 agent 后需要重新拉 state 并重画侧栏。
// 单独暴露成函数而不是让 p14 自己 fetch，是因为 state 是 app.js 的模块级变量，
// 从外面直接改会和之后的 push_state 覆盖互相打架。
window.__renderAgents = async function () {
  try {
    state = await (await fetch('/api/state')).json();
    order = state.order || order;
    renderAgents();
  } catch (e) {
    console.warn('[renderAgents] 刷新失败', e);
  }
};

function turnHtml(t) {
  const isHuman = t.speaker === 'human';
  const a = state && state.agents.find(x => x.id === t.speaker);
  // 人类用中性浅色：Pi 的配置色正好也是 #6ea8fe，两边同色的话
  // 色条就分不出"我说的"和"Pi 说的"。人类已经靠卡片底色（蓝）区分，够明显了。
  const color = safeColor(isHuman ? '#e6edf3' : (a ? a.color : '#888'));
  const cls = 'turn' + (isHuman ? ' human' : '') + (t.status === 'error' ? ' err' : '');
  const tk = t.tokens ? `<span class="tk">${t.tokens} tok</span>` : '';
  const secs = t.ms ? (t.ms / 1000) : 0;
  const slow = secs >= 20 ? ` <span class="slow" title="这一轮比其它人慢很多">${secs.toFixed(1)}s 慢</span>`
                         : (secs ? ` <span class="tk">${secs.toFixed(1)}s</span>` : '');
  const tm = new Date((t.ts || 0) * 1000).toLocaleTimeString('zh-CN', {hour12: false});
  // G4：每句话带上它出自哪个模型。同一个人换过模型后正文风格会跳变，
  // 没有这个戳就只能靠猜；猜错的复盘比没复盘更糟。
  const mdl = t.model ? ` <span class="mdl" title="这一轮用的模型">${esc(t.model)}</span>` : '';
  // --c 把说话人的颜色交给 CSS：左侧色条、头像光晕、悬停高亮都从这里取。
  // 只在这一处传，样式表里就不用再写死任何 agent 的颜色。
  return `<div class="${cls}" style="--c:${color}">
    <div class="av" style="background-color:${color}">${esc(initials(t.name))}</div>
    <div class="body">
      <div class="who"><span class="nm">${esc(t.name)}</span>
        <span class="tm">${tm}</span>${mdl}${tk}${slow}</div>
      <div class="txt">${md(t.text)}</div>
    </div>
  </div>`;
}

function renderAll() {
  if (!state) return;
  Object.keys(timers).forEach(stopTimer);
  logEl.innerHTML = state.turns.map(turnHtml).join('');
  renderAgents();
  setBusy(state.running);
  scrollBottom();
}

function scrollBottom() {
  logEl.scrollTop = logEl.scrollHeight;
}

function liveEl(id) {
  let el = document.getElementById('live-' + id);
  if (!el) {
    const a = state && state.agents.find(x => x.id === id);
    const color = a ? a.color : '#888';
    el = document.createElement('div');
    el.id = 'live-' + id;
    el.className = 'turn';
    el.innerHTML = `
      <div class="av" style="background:${color}">${esc(initials(a ? a.name : id))}</div>
      <div class="body">
        <div class="who"><span class="nm" style="color:${color}">${esc(a ? a.name : id)}</span>
          <span class="elapsed">0.0s</span></div>
        <div class="tools"></div>
        <div class="txt"><span class="typing">…</span><span class="caret"></span></div>
      </div>`;
    logEl.appendChild(el);
    startTimer(id, el);
  }
  return el;
}

// 每个正在说话的 agent 一个计时器：慢不慢，用眼睛看不出来，得给数字。
const timers = {};
function startTimer(id, el) {
  const t0 = performance.now();
  timers[id] = setInterval(() => {
    const box = el.querySelector('.elapsed');
    if (!box) return clearInterval(timers[id]);
    const s = (performance.now() - t0) / 1000;
    box.textContent = s.toFixed(1) + 's';
    if (s >= 20) box.style.color = 'var(--err)';
  }, 100);
}
function stopTimer(id) {
  if (timers[id]) { clearInterval(timers[id]); delete timers[id]; }
}

function setBusy(on) {
  sendEl.disabled = false;   // 允许在 agents 说话时插话（会被排队到下一轮）
  stopEl.disabled = !on;
  statusEl.textContent = on ? 'agents 发言中…' : '就绪';
}

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    handle(m);
  };
  ws.onclose = () => { statusEl.textContent = '连接断开，3 秒后重连…'; setTimeout(connect, 3000); };
}

function handle(m) {
  if (m.type === 'exported') {
    statusEl.textContent = `已导出：${m.path}`;
    statusEl.style.color = 'var(--ok)';
    setTimeout(() => { statusEl.textContent = '就绪'; statusEl.style.color = ''; }, 8000);
    return;
  }
  // 回复守卫拦下一条时，界面要说明"它为什么没说话"。
  // 被拦下的回复不进转录（那本来就不是发言），所以不能靠消息流体现——
  // 没有这条提示的话，用户只会看到某个 agent 莫名其妙地沉默。
  if (m.type === 'notice') {
    statusEl.textContent = m.text || '有一轮被拦下';
    statusEl.style.color = m.level === 'warn' ? 'var(--warn)' : 'var(--dim)';
    clearTimeout(window.__noticeTimer);
    window.__noticeTimer = setTimeout(() => {
      statusEl.textContent = state && state.running ? 'agents 发言中…' : '就绪';
      statusEl.style.color = '';
    }, 12000);
    return;
  }
  if (m.type === 'state') {
    state = m;
    if (m.order) order = m.order.slice();
    document.getElementById('cwd').textContent = m.cwd || '';
    const saved = document.getElementById('saved');
    if (saved && m.count) saved.textContent = `已保存 ${m.count} 条`;
    renderAll();
    return;
  }
  if (m.type === 'turn_start') {
    live[m.agent] = {name: m.name, text: '', tools: []};
    const old = document.getElementById('live-' + m.agent);
    if (old) old.remove();
    liveEl(m.agent);
    scrollBottom();
    return;
  }
  if (m.type === 'chunk') {
    const el = liveEl(m.agent);
    const txt = el.querySelector('.txt');
    const cur = (live[m.agent] && live[m.agent].text || '') + (m.text || '');
    if (live[m.agent]) live[m.agent].text = cur;
    txt.innerHTML = md(cur) + '<span class="caret"></span>';
    scrollBottom();
    return;
  }
  if (m.type === 'tool') {
    const el = liveEl(m.agent);
    const box = el.querySelector('.tools');
    const cls = 'tool ' + (m.status || '');
    let chip = [...box.children].find(c => c.dataset.title === m.title);
    if (!chip) {
      chip = document.createElement('span');
      chip.dataset.title = m.title;
      box.appendChild(chip);
    }
    chip.className = cls;
    chip.textContent = m.title + (m.status ? ' · ' + m.status : '');
    scrollBottom();
    return;
  }
  if (m.type === 'turn_end') {
    const id = m.turn.speaker;
    stopTimer(id);
    const liveBox = document.getElementById('live-' + id);
    if (liveBox) liveBox.remove();
    logEl.insertAdjacentHTML('beforeend', turnHtml(m.turn));
    scrollBottom();
    return;
  }
  if (m.type === 'permission') {
    // P9 内联审批（claude）：P9_INLINE=true 时不弹全屏模态，由 p9-inline.js
    // 就地画卡片；p9-inline.js 没加载（或手动关掉内联）时落回原模态。只加守卫。
    if (window.P9_INLINE && window.p9Inline) { window.p9Inline(m); return; }
    showPermission(m);
    return;
  }
}

function showPermission(m) {
  document.getElementById('perm-title').textContent = `${m.agent} 请求权限`;
  document.getElementById('perm-desc').textContent = m.tool || '工具调用';
  const opts = document.getElementById('perm-opts');
  opts.innerHTML = '';
  (m.options || []).forEach(o => {
    const b = document.createElement('button');
    b.textContent = o.name || o.optionId;
    b.onclick = () => {
      ws.send(JSON.stringify({type: 'permission', id: m.id, optionId: o.optionId}));
      document.getElementById('perm').classList.remove('show');
    };
    opts.appendChild(b);
  });
  const c = document.createElement('button');
  c.textContent = '拒绝';
  c.style.color = 'var(--err)';
  c.onclick = () => {
    ws.send(JSON.stringify({type: 'permission', id: m.id, optionId: '__cancel__'}));
    document.getElementById('perm').classList.remove('show');
  };
  opts.appendChild(c);
  document.getElementById('perm').classList.add('show');
}

function send() {
  const text = inputEl.value.trim();
  if (!text) return;
  ws.send(JSON.stringify({
    type: 'say', text,
    auto: document.getElementById('auto').checked,
    rounds: +document.getElementById('rounds').value || 1,
    speakers: order
  }));
  inputEl.value = '';
  inputEl.style.height = 'auto';
}

function stopAll() {
  ws.send(JSON.stringify({type: 'stop'}));
}

async function reconnect() {
  await fetch('/api/reconnect', {method: 'POST'});
}

function exportMd() {
  ws.send(JSON.stringify({type: 'export'}));
}

async function clearAll() {
  if (!confirm('清空当前对话？旧记录会改名留档，不会真删。')) return;
  await fetch('/api/clear', {method: 'POST'});
}

// G6：把房间的结论写进各 agent 自己的长期记忆文件。
// 为什么先拉一次 /api/memory 再确认：落点是各家不同的文件
// （~/.claude/CLAUDE.md、~/.codex/AGENTS.md、Hermes 的 memories/MEMORY.md…），
// 点一下就在别人目录下动文件，必须先把路径摆出来让人看一眼再动手。
// 没有落点的（那个 agent 没装）列出来反而比藏起来好——否则用户以为都写了。
async function promoteMemory() {
  statusEl.textContent = '正在看各 agent 的记忆落在哪里…';
  let targets = [];
  try {
    const d = await (await fetch('/api/memory')).json();
    targets = (d.agent_memory && d.agent_memory.targets) || [];
  } catch (e) {
    statusEl.textContent = '读不到落点：' + e;
    return;
  }
  if (!targets.length) { statusEl.textContent = '这个房间里没有 agent'; return; }
  const lines = targets.map(t => t.exists
    ? `• ${t.agent} → ${t.path}` + (t.chars ? `（${t.chars}）` : (t.present ? '（已有我写的块）' : ''))
    : `• ${t.agent} —— 跳过：${t.why}`);
  if (!targets.some(t => t.exists)) { statusEl.textContent = '没有可写的落点'; return; }
  if (!confirm('把房间的结论写进这些文件？\n只改 acp-room 自己那一段，你手写的部分不动；\n写之前各存一份备份到房间的 data 目录。\n生效时机：各 agent 的下一次会话。\n\n' + lines.join('\n'))) {
    statusEl.textContent = '就绪';
    return;
  }
  statusEl.textContent = '写入中…';
  const res = await fetch('/api/memory/promote', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const d = await res.json().catch(() => ({ok: false, error: '返回不是 JSON'}));
  if (!d.ok) { statusEl.textContent = '写入失败：' + (d.error || res.status); return; }
  const rs = d.results || [];
  const n = k => rs.filter(r => r.status === k).length;
  // 每个 agent 一行原因。跳过和失败必须点名，否则"跳过 3"到底为什么没写，
  // 还得去翻审计 —— 而这正是这一行唯一要说清楚的事。
  const bad = rs.filter(r => r.status === 'failed' || r.status === 'skipped');
  const tail = bad.length
    ? '｜' + bad.map(r => `${r.agent}：${r.status === 'failed' ? '失败' : '跳过'}（${r.detail}）`).join(' ')
    : '';
  statusEl.textContent =
    `写入${n('written')}、未变${n('unchanged')}、跳过${n('skipped')}、失败${n('failed')}`
    + '（生效时机是它们的下一次会话）' + tail;
}

inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + 'px';
});

connect();
