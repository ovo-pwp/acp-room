/* P2 讨论引擎前端 —— 阶段实时显示 + 讨论法/预算控件。归 omp。
 *
 * 合并方式（pi 负责）：index.html 里 app.js 之后加一行
 *   <script src="/static/p2-discuss.js"></script>
 *
 * 设计约束：不许改 app.js / index.html，所以这个文件完全自含——
 * 1. 自开一条 WebSocket：server 把所有连接都放进 ROOM.sockets 广播，天然能收到
 *    state / notice / turn_* 全部消息。只处理自己关心的，其余一律忽略，
 *    权限弹窗等仍由 app.js 处理，不会双重渲染。
 * 2. 阶段显示靠后端的 room.announce()：后端每次切阶段都会广播
 *      {"type":"notice","text":"阶段：discuss · 第 2/4 轮"}
 *    结束时广播 {"type":"notice","text":"讨论结束：<原因>"}。
 *    原来的 app.js 根本没处理 notice 类型（读了 app.js 全文确认），所以阶段
 *    切换一直是"后端在喊、前端装聋"。这里补上。
 * 3. 发起讨论走现成的 POST /api/discuss，停止走 POST /api/stop，不需要新接口。
 */

(function () {
  'use strict';

  // 三种讨论法的阶段轨道。后端 announce 的「阶段：<key>」按这里高亮。
  var TRACKS = {
    standard: ['answer', 'critique', 'discuss', 'position', 'synthesis'],
    advocate: ['answer', 'critique', 'challenge', 'position', 'synthesis'],
    oxford:   ['opening', 'rebuttal', 'closing', 'synthesis'],
  };
  var LABEL = {
    answer: '①独立作答', critique: '②互挑错', discuss: '③交锋', position: '④表态',
    synthesis: '⑤汇总', challenge: '③魔鬼代言人', opening: '①立论', rebuttal: '②驳论',
    closing: '③结辩',
  };
  var METHOD_NAME = { standard: 'Standard 五阶段', advocate: 'Advocate 魔鬼代言人', oxford: 'Oxford 辩论' };
  // 停因上色：kind 不走后端，按 reason 关键词判（后端 reason 文案稳定）。
  var STOP_COLOR = [
    [/CONSENSUS|收敛/, 'var(--ok)'],
    [/DEADLOCK|死锁/, 'var(--err)'],
    [/没有新论点|原地打转/, 'var(--warn)'],
    [/超时|预算/, 'var(--warn)'],
  ];

  var ws = null;
  var running = false;
  var els = {};

  function esc(s) {
    // 和 app.js 一致：先 String()。`(s || '')` 会把 0/false 静默变成空串，
    // 数字直接 .replace 还会 TypeError。这里现在只拿字符串，但别留这个雷。
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function injectStyle() {
    var css = [
      '.p2-panel{border-top:1px solid var(--line);padding:8px 12px;display:grid;gap:6px;',
      '  background:color-mix(in srgb, var(--line) 18%, transparent);font-size:12.5px}',
      '.p2-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}',
      '.p2-row label{display:flex;align-items:center;gap:4px;color:var(--fg-dim, #9aa4b2)}',
      '.p2-panel select,.p2-panel input{background:transparent;color:inherit;',
      '  border:1px solid var(--line);border-radius:6px;padding:2px 6px}',
      '.p2-panel input{width:64px}',
      '.p2-track{display:flex;flex-wrap:wrap;gap:6px;align-items:center;min-height:22px}',
      '.p2-chip{border:1px solid var(--line);border-radius:999px;padding:1px 10px;',
      '  color:var(--fg-dim, #9aa4b2);white-space:nowrap}',
      '.p2-chip.cur{color:#0b0e14;background:var(--ok,#7ee787);border-color:transparent;font-weight:600}',
      '.p2-chip.done{color:var(--ok,#7ee787);border-color:var(--ok,#7ee787)}',
      '.p2-detail{color:var(--warn,#ffb86c)}',
      '.p2-stop{display:none;padding:2px 8px;border-radius:6px;border:1px solid var(--line)}',
      '.p2-go{background:var(--ok,#7ee787);color:#0b0e14;border:none;border-radius:6px;',
      '  padding:3px 12px;font-weight:600;cursor:pointer}',
      '.p2-go:disabled{opacity:.45;cursor:not-allowed}',
      '.p2-halt{background:transparent;color:var(--err);border:1px solid var(--err);',
      '  border-radius:6px;padding:3px 10px;cursor:pointer}',
    ].join('');
    var st = document.createElement('style');
    st.id = 'p2-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  function buildPanel() {
    var panel = document.createElement('div');
    panel.className = 'p2-panel';
    panel.id = 'p2-panel';
    panel.innerHTML =
      '<div class="p2-row">' +
      '  <b>讨论引擎</b>' +
      '  <select id="p2-method">' +
      '    <option value="standard">Standard 五阶段</option>' +
      '    <option value="advocate">Advocate 魔鬼代言人</option>' +
      '    <option value="oxford">Oxford 辩论（正反方）</option>' +
      '  </select>' +
      '  <label>轮上限 <input type="number" id="p2-rounds" value="4" min="1" max="8"></label>' +
      '  <label>token 预算 <input type="number" id="p2-tokens" value="200" min="10" max="2000">k</label>' +
      '  <button class="p2-go" id="p2-go">▶ 用上面的问题开一场讨论</button>' +
      '  <button class="p2-halt" id="p2-halt">停止</button>' +
      '</div>' +
      '<div class="p2-track" id="p2-track"></div>' +
      '<div class="p2-row"><span id="p2-detail" class="p2-detail"></span>' +
      '  <span id="p2-stop" class="p2-stop"></span></div>';

    var main = document.querySelector('main');
    var footer = main && main.querySelector('footer');
    if (!main || !footer) return false;
    main.insertBefore(panel, footer);

    els.panel = panel;
    els.method = panel.querySelector('#p2-method');
    els.rounds = panel.querySelector('#p2-rounds');
    els.tokens = panel.querySelector('#p2-tokens');
    els.go = panel.querySelector('#p2-go');
    els.halt = panel.querySelector('#p2-halt');
    els.track = panel.querySelector('#p2-track');
    els.detail = panel.querySelector('#p2-detail');
    els.stop = panel.querySelector('#p2-stop');

    els.go.onclick = startDiscuss;
    els.halt.onclick = function () {
      fetch('/api/stop', { method: 'POST' });
    };
    els.method.onchange = function () { renderTrack(); };
    renderTrack();
    return true;
  }

  function renderTrack(curKey, detailText) {
    var track = TRACKS[els.method.value] || TRACKS.standard;
    var seen = curKey ? track.indexOf(curKey) : -1;
    els.track.innerHTML = track.map(function (k, i) {
      var cls = 'p2-chip' + (k === curKey ? ' cur' : (seen > i ? ' done' : ''));
      return '<span class="' + cls + '">' + esc(LABEL[k] || k) + '</span>';
    }).join('<span style="opacity:.4">→</span>');
    els.detail.textContent = detailText || '';
  }

  function startDiscuss() {
    var input = document.getElementById('input');
    var topic = (input && input.value.trim()) || '';
    if (!topic) {
      showStop('先在下面的输入框写议题，再点这里开讨论。', 'var(--warn)');
      return;
    }
    var body = {
      topic: topic,
      method: els.method.value,
      max_rounds: parseInt(els.rounds.value, 10) || 4,
      max_tokens: (parseInt(els.tokens.value, 10) || 200) * 1000,
    };
    els.go.disabled = true;
    els.stop.style.display = 'none';
    els.detail.textContent = '发起中…';
    fetch('/api/discuss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (!res.ok || res.j.ok === false) {
        els.go.disabled = false;
        els.detail.textContent = '';
        showStop('发起失败：' + (res.j.error || res.j.detail || ('HTTP ' + res.status)), 'var(--err)');
        return;
      }
      if (input) input.value = '';
      renderTrack();               // 清空高亮，等后端第一条「阶段：」来点亮
      els.detail.textContent = METHOD_NAME[body.method] + ' 进行中…';
    }).catch(function (e) {
      els.go.disabled = false;
      els.detail.textContent = '';
      showStop('发起失败：' + e, 'var(--err)');
    });
  }

  function showStop(reason, color) {
    els.stop.textContent = '■ ' + reason;
    els.stop.style.display = 'inline-block';
    els.stop.style.color = color || '';
    els.stop.style.borderColor = color || '';
  }

  // 阶段通知解析。「阶段：X · 说明」→ 高亮 chip X + 说明文字；
  // 「讨论结束：原因」→ 展示停因（spec 硬要求：停止必须显示原因）。
  function onNotice(text) {
    var m = text.match(/^阶段：(.+?)(?:\s·\s(.*))?$/);
    if (m) {
      var key = m[1].trim().split(/\s+/)[0];
      renderTrack(key, m[2] || '');
      if (TRACKS[els.method.value] && TRACKS[els.method.value].indexOf(key) < 0) {
        // 后端阶段与面板方法不符（比如服务端 method 被别人改了）：跟着后端走
        for (var meth in TRACKS) {
          if (TRACKS[meth].indexOf(key) >= 0 && meth !== els.method.value) {
            els.method.value = meth;
            renderTrack(key, m[2] || '');
            break;
          }
        }
      }
      return;
    }
    m = text.match(/^讨论结束：(.+)$/);
    if (m) {
      var color = '';
      for (var i = 0; i < STOP_COLOR.length; i++) {
        if (STOP_COLOR[i][0].test(m[1])) { color = STOP_COLOR[i][1]; break; }
      }
      showStop(m[1], color);
      els.detail.textContent = '';
      els.go.disabled = false;
      return;
    }
    if (text.indexOf('已经跑了') >= 0 || text.indexOf('已在跑') >= 0) {
      els.go.disabled = false;
      els.detail.textContent = '';
      showStop(text, 'var(--err)');
    }
  }

  function connect() {
    ws = new WebSocket('ws://' + location.host + '/ws');
    ws.onmessage = function (e) {
      var m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m.type === 'notice') {
        onNotice(m.text || '');
        return;
      }
      if (m.type === 'state') {
        var was = running;
        running = !!m.running;
        if (!running && was) {           // 一场讨论结束（可能没走 announce 兜底）
          els.go.disabled = false;
        }
      }
    };
    ws.onclose = function () { setTimeout(connect, 3000); };
  }

  function init() {
    injectStyle();
    if (!buildPanel()) {
      // DOM 还没就绪（脚本先于 body 加载）等一下再试
      setTimeout(init, 200);
      return;
    }
    connect();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
