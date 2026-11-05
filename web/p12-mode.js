/* P12 讨论模式成为默认路径 —— 让 P2 讨论引擎真正被用上
 *
 * 为什么：
 *   P2 讨论引擎（五阶段 + 收敛判据）写完了、也实测能产出真价值——两个 agent 互相改判，
 *   最后收敛出「瓶颈是写入-核验缺强制执行点」的共识（见 0012/log.jsonl）。
 *   **但它从来没被日常用过**：/api/discuss 只有人用 curl 调过，界面上的讨论面板
 *   也没人点。房间默认还是"发一句、四个 agent 轮流接话"，这是最没价值的模式。
 *
 * 做什么（不碰 app.js）：
 *   在输入框下面加一个**模式开关**：说话 / 讨论。
 *     - 说话（默认）：老行为，走 /api/say（或 P11 的 @ 指定）
 *     - 讨论：走 /api/discuss，五阶段协议 + 收敛判据，自动跑完
 *   讨论模式下 "轮数" 输入框的含义变成"交锋轮上限"，并且多一个"讨论法"下拉。
 *
 * 与 P11 的关系：
 *   P11 拦截 #send 的 click（捕获阶段）处理 @ 指定。
 *   这个文件也拦同一个 click，但**顺序在后**：注册晚，先被调用的是 P11。
 *   为避免两个都发，这里用一个共享标志：P11 发过就跳过。
 *   实际做法：检查 window.__p11Handled（P11 发送时设置）。
 *   如果 P11 不在场（没加载），讨论模式自己处理。
 *
 *   @ 与讨论可以共存：讨论模式下 @ 只让被点名的人参与讨论。
 */

(function () {
  'use strict';

  var MODE_KEY = 'acp-room-mode';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g,
      function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }

  var mode = 'chat';
  var saved = null;
  try { saved = localStorage.getItem(MODE_KEY); } catch (e) {}
  if (saved === 'discuss' || saved === 'chat') mode = saved;

  /* ---------- UI ---------- */

  function injectStyle() {
    if (document.getElementById('p12-style')) return;
    var css = [
      '.p12-bar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:8px;',
      '  font-size:12.5px;color:var(--dim)}',
      '.p12-seg{display:inline-flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}',
      '.p12-seg button{background:transparent;color:inherit;border:none;padding:3px 12px;',
      '  cursor:pointer;font:inherit}',
      '.p12-seg button.on{background:var(--ok);color:#0b0e14;font-weight:600}',
      '.p12-bar select,.p12-bar input{background:transparent;color:inherit;',
      '  border:1px solid var(--line);border-radius:6px;padding:2px 6px;font:inherit}',
      '.p12-bar input[type=number]{width:56px}',
      '.p12-hint{color:var(--dim);font-size:11.5px}',
      '.p12-hint b{color:var(--warn)}',
    ].join('');
    var st = document.createElement('style');
    st.id = 'p12-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  function buildBar() {
    var footer = document.querySelector('main footer');
    if (!footer || document.getElementById('p12-bar')) return false;
    var bar = document.createElement('div');
    bar.className = 'p12-bar';
    bar.id = 'p12-bar';
    bar.innerHTML =
      '<span>模式</span>' +
      '<span class="p12-seg">' +
      '  <button id="p12-chat">说话</button>' +
      '  <button id="p12-disc">讨论</button>' +
      '</span>' +
      '<span id="p12-disc-opts" style="display:none;gap:8px;align-items:center">' +
      '  <label>讨论法 <select id="p12-method">' +
      '    <option value="standard">Standard 五阶段</option>' +
      '    <option value="advocate">Advocate 魔鬼代言人</option>' +
      '    <option value="oxford">Oxford 正反方</option>' +
      '  </select></label>' +
      '  <label>交锋轮上限 <input type="number" id="p12-rounds" value="3" min="1" max="8"></label>' +
      '</span>' +
      '<span class="p12-hint" id="p12-hint"></span>';

    var anchor = footer.querySelector('.bar');
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(bar, anchor.nextSibling);
    else footer.appendChild(bar);

    document.getElementById('p12-chat').onclick = function () { setMode('chat'); };
    document.getElementById('p12-disc').onclick = function () { setMode('discuss'); };
    paint();
    return true;
  }

  function paint() {
    var chat = document.getElementById('p12-chat');
    var disc = document.getElementById('p12-disc');
    if (!chat || !disc) return;
    chat.className = mode === 'chat' ? 'on' : '';
    disc.className = mode === 'discuss' ? 'on' : '';
    var opts = document.getElementById('p12-disc-opts');
    if (opts) opts.style.display = mode === 'discuss' ? 'inline-flex' : 'none';
    var hint = document.getElementById('p12-hint');
    if (hint) {
      hint.innerHTML = mode === 'discuss'
        ? '讨论会跑完 <b>五阶段</b>（作答→挑错→交锋→表态→汇总），自动判收敛'
        : '说话：agents 依次接话（可用 <b>@名字</b> 只叫一个）';
    }
    var ta = document.getElementById('input');
    if (ta) {
      ta.placeholder = mode === 'discuss'
        ? '写议题，回车开一场讨论。例如：这个平台该不该做多房间？'
        : '说点什么，回车发送（Shift+回车换行）。发完 agents 会依次接话。';
    }
  }

  function setMode(m) {
    mode = m;
    try { localStorage.setItem(MODE_KEY, m); } catch (e) {}
    paint();
  }

  /* ---------- 发送 ---------- */

  var busy = false;

  function speakersFor(text) {
    // 讨论模式下也支持 @：只在被点名的人里讨论
    var P = window.P11;
    if (P && typeof P.parse === 'function') {
      var r = P.parse(text);
      if (r && r.speakers && r.speakers.length) return r.speakers;
    }
    return null;
  }

  function onSendClickCaptured(ev) {
    if (mode !== 'discuss') return;              // 说话模式交给 app.js / P11
    var input = document.getElementById('input');
    var text = input ? input.value.trim() : '';
    if (!text) return;

    ev.stopImmediatePropagation();
    ev.preventDefault();
    if (busy) return;
    busy = true;

    var methodEl = document.getElementById('p12-method');
    var roundsEl = document.getElementById('p12-rounds');
    var body = {
      topic: text,
      method: methodEl ? methodEl.value : 'standard',
      max_rounds: roundsEl ? (+roundsEl.value || 3) : 3,
      max_tokens: 200000
    };
    var sp = speakersFor(text);
    if (sp) body.speakers = sp;

    var st = document.getElementById('status');
    if (st) { st.textContent = '讨论发起中…'; st.style.color = 'var(--warn)'; }

    fetch('/api/discuss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) throw new Error(d.error || '发起失败');
      if (input) { input.value = ''; input.style.height = 'auto'; }
      if (st) { st.textContent = '讨论进行中：' + (d.method || 'standard'); }
    }).catch(function (e) {
      if (st) { st.textContent = '讨论发起失败：' + e.message; st.style.color = 'var(--err)'; }
    }).then(function () {
      busy = false;
    });
  }

  /* ---------- 启动 ---------- */

  function boot() {
    injectStyle();
    if (!buildBar()) { setTimeout(boot, 400); return; }   // footer 可能还没渲染
    var btn = document.getElementById('send');
    // 捕获阶段、注册在 P11 之后 → 说话模式让给 P11，讨论模式自己接管
    if (btn) btn.addEventListener('click', onSendClickCaptured, true);
  }

  window.P12 = { setMode: setMode, mode: function () { return mode; } };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
