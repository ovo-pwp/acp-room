/* P11 `@` 指定发言人 —— 只让被点到的人回话
 *
 * 为什么要这个：
 *   现在发一句话，全房间四个 agent 一起被拉起来。实测代价——聊一句好烧 7.4 万 token、
 *   等 4 分 20 秒（OMP 一家 263s 占 95%）。而 90% 的时候你只想问其中一个人。
 *   HANDOFF.md 里那条"一个权限弹窗卡住整条流水线"，根源也是"一发消息全员起床"。
 *
 * 怎么做（不碰 app.js）：
 *   app.js 的 send() 里 speakers 写死为全房间 order，我们拦不到它内部的变量。
 *   所以用两条互补的路：
 *     1) 捕获阶段监听 #send 的 click，在 app.js 的 onclick 之前把 @名字 解析出来，
 *        直接把改写后的消息自己发出去，并阻止原 handler（stopImmediatePropagation）。
 *     2) 如果按钮不是 onclick 而是 addEventListener 绑的，第 1 条不生效——
 *        那就用 patchWebSocket：包一层 ws.send，凡是 {type:'say'} 且带 @ 的，
 *        发送前替换 speakers。
 *   两条路都可能命中，用 lastSent 去重，绝不会发两次。
 *
 * 语法：
 *   @codex 你看这个        → 只有 codex 回
 *   @claude @omp 对比一下   → claude 和 omp 都回
 *   @all 大家怎么看        → 全房间（等于不写）
 *   你好（无 @）           → 全房间，行为不变
 *
 * 名字匹配是宽松的：@cc → claude，@c → codex/claude 都匹配则按最长唯一前缀。
 */

(function () {
  'use strict';

  var api = { lastSpeakers: null };

  function agentIds() {
    // app.js 把当前房间状态放在全局 state（见 app.js 顶部 `let state = null`）
    try {
      if (window.state && window.state.agents) return window.state.agents.map(function (a) { return a.id; });
    } catch (e) {}
    return [];
  }

  function agentNames() {
    try {
      if (window.state && window.state.agents) {
        return window.state.agents.map(function (a) { return { id: a.id, name: (a.name || a.id) }; });
      }
    } catch (e) {}
    return [];
  }

  /* 把一个 @token 解析成 agent id。宽松匹配：
   *   - 精确 id 优先
   *   - 否则 name 小写精确
   *   - 否则唯一前缀（@cl → claude）
   * 返回 null 表示不是已知 agent（当普通文字保留，不删）。 */
  function resolve(token) {
    var t = String(token || '').toLowerCase().trim();
    if (!t) return null;
    var ids = agentIds();
    var names = agentNames();
    if (t === 'all' || t === '全体' || t === '大家') return '__all__';
    if (ids.indexOf(t) >= 0) return t;
    for (var i = 0; i < names.length; i++) {
      if (String(names[i].name).toLowerCase() === t) return names[i].id;
    }
    var hit = ids.filter(function (id) { return id.toLowerCase().indexOf(t) === 0; });
    if (hit.length === 1) return hit[0];
    return null;
  }

  /* 从文本里抽出 @谁，返回 {speakers, cleaned}
   * cleaned 去掉 @token 但保留其余文字（@all 和未知 @ 也不删，只是不当指令）。 */
  function parse(text) {
    var ids = agentIds();
    if (!ids.length) return { speakers: null, cleaned: text, hit: [] };
    var seen = {};
    var hit = [];
    // @ 后面跟字母数字/_/-，或中文名字
    var re = /@([A-Za-z0-9_\-]+|[\u4e00-\u9fff]{1,8})/g;
    var m;
    while ((m = re.exec(text)) !== null) {
      var r = resolve(m[1]);
      if (r === '__all__') { seen['__all__'] = 1; continue; }
      if (r && !seen[r]) { seen[r] = 1; hit.push(r); }
    }
    if (seen['__all__']) return { speakers: null, cleaned: text, hit: [] };  // @all = 全房间
    if (!hit.length) return { speakers: null, cleaned: text, hit: [] };
    // 只保留房间里真实存在的
    var speakers = hit.filter(function (id) { return ids.indexOf(id) >= 0; });
    if (!speakers.length) return { speakers: null, cleaned: text, hit: [] };
    return { speakers: speakers, cleaned: text, hit: speakers };
  }

  /* ---------- 路由 1：拦 #send 的 click（app.js 用的是 onclick） ---------- */

  var lastSent = '';
  function alreadySent(text) {
    if (lastSent === text) { lastSent = ''; return true; }
    return false;
  }

  function onSendClick(ev) {
    if (yieldingToP12()) return;              // 讨论模式让给 P12
    var input = document.getElementById('input');
    var text = input ? input.value.trim() : '';
    if (!text) return;
    var r = parse(text);
    if (!r.speakers) return;                  // 没点名 → 交给 app.js 原逻辑
    ev.stopImmediatePropagation();            // 别让 app.js 再发一次
    ev.preventDefault();
    var autoEl = document.getElementById('auto');
    var roundsEl = document.getElementById('rounds');
    var body = {
      text: r.cleaned,                        // 原文照发（@ 留着，人和 agent 都能看见）
      auto: autoEl ? autoEl.checked : true,
      rounds: roundsEl ? (+roundsEl.value || 1) : 1,
      speakers: r.speakers
    };
    // app.js 的 ws 是 `let` 声明，拿不到实例（实测 window.ws 为 undefined），
    // 所以这里走后端同款 HTTP 接口 /api/say——它内部做的事和 WS 的 say 分支一样。
    // 好处：不依赖任何内部变量；代价：没有瞬时反馈，靠 3 秒一次的 state 推送刷新。
    fetch('/api/say', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).catch(function () {
      var st = document.getElementById('status');
      if (st) { st.textContent = '发送失败'; st.style.color = 'var(--err)'; }
    });
    input.value = '';
    input.style.height = 'auto';
    showHint(r.speakers);
  }

  /* ---------- 路由 2：拦 WebSocket.prototype.send
   *
   * app.js 里是 `let ws = null`，块级声明，**不会挂到 window 上**（实测确认）。
   * 所以 window.ws 拿不到实例，只能拦原型方法——这是唯一能捕获 app.js
   * 自己发出的帧的地方。
   * ---------- */

  function patchProto() {
    var P = window.WebSocket && window.WebSocket.prototype;
    if (!P || P.__p11) return;
    var orig = P.send;
    P.send = function (payload) {
      try {
        var m = JSON.parse(payload);
        if (m && m.type === 'say' && typeof m.text === 'string') {
          var r = parse(m.text);
          if (r.speakers) {
            if (alreadySent(m.text)) return;     // 路由 1 已发过
            m.speakers = r.speakers;
            payload = JSON.stringify(m);
            showHint(r.speakers);
          }
        }
      } catch (e) { /* 不是 JSON 就原样发 */ }
      return orig.call(this, payload);
    };
    P.__p11 = true;
  }

  /* P12 讨论模式存在时，@ 不该由这里接管——否则讨论模式会被降级成说话。
   *
   * 不能只看 window.P12——它的加载顺序可能晚于本文件，click 那一刻不一定存在。
   * 直接看 DOM（P12 切到讨论时会给 #p12-disc 加 .on），这个总是同步可靠。 */
  function yieldingToP12() {
    var disc = document.getElementById('p12-disc');
    return !!(disc && disc.classList.contains('on'));
  }

  /* ---------- 反馈：@ 了谁要看得见 ---------- */

  function showHint(speakers) {
    var st = document.getElementById('status');
    if (!st) return;
    var names = agentNames();
    var label = speakers.map(function (id) {
      var f = names.filter(function (n) { return n.id === id; })[0];
      return f ? f.name : id;
    }).join('、');
    st.textContent = '只叫 ' + label;
    st.style.color = 'var(--ok)';
    setTimeout(function () {
      if (st.textContent.indexOf('只叫 ') === 0) {
        st.textContent = '就绪';
        st.style.color = '';
      }
    }, 4000);
  }

  /* ---------- 启动 ---------- */

  function boot() {
    patchProto();
    var btn = document.getElementById('send');
    if (btn) btn.addEventListener('click', onSendClick, true);   // 捕获阶段，先于 onclick
    // ws 可能还没建好（app.js 的 connect 是异步的）——但 prototype
    // 拦的是构造函数，早于实例创建，一次就够了。这里只做保险。
    var tries = 0;
    var t = setInterval(function () {
      patchProto();
      if (window.WebSocket && window.WebSocket.prototype.__p11) clearInterval(t);
      if (++tries > 60) clearInterval(t);
    }, 500);

    // 输入框里打 @ 时提示可选的名字
    var input = document.getElementById('input');
    if (input) {
      input.addEventListener('input', function () {
        var v = input.value;
        var at = v.lastIndexOf('@');
        if (at < 0) return;
        var frag = v.slice(at + 1);
        if (/[\s]/.test(frag)) return;
        var names = agentNames();
        var cand = names.filter(function (n) {
          return n.id.toLowerCase().indexOf(frag.toLowerCase()) === 0 ||
                 String(n.name).toLowerCase().indexOf(frag.toLowerCase()) === 0;
        });
        if (!cand.length) return;
        var st = document.getElementById('status');
        if (st) { st.textContent = '按 @ 选：' + cand.map(function (c) { return c.id; }).join(' / '); st.style.color = 'var(--dim)'; }
      });
    }
  }

  window.P11 = { parse: parse, resolve: resolve, boot: boot };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
