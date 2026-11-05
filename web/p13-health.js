/* P13 健康面板 —— 一屏看清房间的真实状态
 *
 * 为什么：
 *   房间的状态本来分散在 /api/state、/api/options、/api/audit、/api/agents
 *   和三个日志文件里。想知道“为什么这轮慢”“哪个配置是错的”，得挨个 curl 拼图。
 *
 *   更要紧的是：它会把“配置和实际不一致”直接摆出来。比如
 *     - omp 的 mode=plan（只读），却没人知道自己被限制了
 *     - omp 的 model 配成一个网关上不存在的名字
 *     - permission=allow 意味着全部自动放行、零审批
 *   这些以前藏在 JSON 里，现在一行一行列出来。
 *
 * 数据：GET /api/health（server.py 新增）。没有这个接口就显示占位，绝不白屏。
 */

(function () {
  'use strict';

  var API = '/api/health';
  var POLL = 5000;
  var collapsed = false;
  try { collapsed = localStorage.getItem('acp-health-collapsed') === '1'; } catch (e) {}

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g,
      function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }
  function sec(ms) {
    if (!ms) return '—';
    return (ms / 1000).toFixed(1) + 's';
  }
  function ktok(n) {
    if (!n) return '—';
    return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
  }
  function bytes(n) {
    if (!n) return '0';
    return n >= 1048576 ? (n / 1048576).toFixed(1) + 'MB'
      : n >= 1024 ? (n / 1024).toFixed(1) + 'KB' : n + 'B';
  }

  function injectStyle() {
    if (document.getElementById('p13-style')) return;
    var css = [
      '#p13-panel{position:fixed;right:0;top:0;bottom:0;width:360px;z-index:40;',
      '  background:var(--panel,#161b22);border-left:1px solid var(--line);',
      '  display:flex;flex-direction:column;font-size:12.5px;transition:transform .18s}',
      '#p13-panel.p13-hide{transform:translateX(360px)}',
      '#p13-tab{position:fixed;right:0;top:50%;z-index:41;writing-mode:vertical-rl;',
      '  padding:10px 5px;background:var(--panel,#161b22);border:1px solid var(--line);',
      '  border-right:none;border-radius:6px 0 0 6px;cursor:pointer;font-size:12px;display:none}',
      '#p13-tab.show{display:block}',
      '.p13-head{display:flex;align-items:center;gap:8px;padding:10px 12px;',
      '  border-bottom:1px solid var(--line)}',
      '.p13-head b{flex:1}',
      '.p13-head button{background:transparent;border:1px solid var(--line);color:inherit;',
      '  border-radius:6px;padding:2px 8px;cursor:pointer;font:inherit}',
      '.p13-body{overflow:auto;padding:8px 12px;flex:1}',
      '.p13-sec{margin:10px 0 4px;color:var(--dim,#9aa4b2);font-size:11px;',
      '  text-transform:uppercase;letter-spacing:.06em}',
      '.p13-agent{border:1px solid var(--line);border-radius:8px;padding:7px 9px;margin-bottom:7px}',
      '.p13-row{display:flex;justify-content:space-between;gap:8px;padding:1px 0}',
      '.p13-row span:first-child{color:var(--dim,#9aa4b2)}',
      '.p13-ok{color:var(--ok,#7ee787)} .p13-warn{color:var(--warn,#ffb86c)}',
      '.p13-err{color:var(--err,#f85149)} .p13-dim{color:var(--dim,#9aa4b2)}',
      '.p13-cfg{font-family:ui-monospace,Consolas,monospace;font-size:11px;',
      '  color:var(--dim,#9aa4b2);word-break:break-all;line-height:1.5}',
      '.p13-warn-box{border:1px solid var(--warn,#ffb86c);border-radius:8px;',
      '  padding:7px 9px;margin-bottom:8px;color:var(--warn,#ffb86c);font-size:11.5px}',
      '.p13-warn-box div{padding:1px 0}',
      '.p13-ph{color:var(--dim,#9aa4b2);padding:14px 4px;line-height:1.6}',
    ].join('');
    var st = document.createElement('style');
    st.id = 'p13-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  function build() {
    if (document.getElementById('p13-panel')) return true;
    var p = document.createElement('div');
    p.id = 'p13-panel';
    p.className = collapsed ? 'p13-hide' : '';
    p.innerHTML =
      '<div class="p13-head"><b>健康面板</b>' +
      '<button id="p13-refresh" title="立刻刷新">↻</button>' +
      '<button id="p13-close" title="收起">»</button></div>' +
      '<div class="p13-body" id="p13-body"><div class="p13-ph">加载中…</div></div>';
    document.body.appendChild(p);

    var tab = document.createElement('div');
    tab.id = 'p13-tab';
    tab.className = collapsed ? 'show' : '';
    tab.textContent = '健康';
    tab.title = '展开健康面板';
    document.body.appendChild(tab);

    document.getElementById('p13-close').onclick = function () { setCollapsed(true); };
    tab.onclick = function () { setCollapsed(false); };
    document.getElementById('p13-refresh').onclick = function () { refresh(); };
    return true;
  }

  function setCollapsed(v) {
    collapsed = v;
    try { localStorage.setItem('acp-health-collapsed', v ? '1' : '0'); } catch (e) {}
    var p = document.getElementById('p13-panel');
    var t = document.getElementById('p13-tab');
    if (p) p.className = v ? 'p13-hide' : '';
    if (t) t.className = v ? 'show' : '';
    // 让正文给这块 360px 的面板让位（见 style.css 的 rail 规则），否则聊天文字被压在底下
    if (document.body) document.body.classList.toggle('p13-rail-open', !v);
  }

  function render(d) {
    var body = document.getElementById('p13-body');
    if (!body) return;
    var h = [];

    if (d.warnings && d.warnings.length) {
      h.push('<div class="p13-warn-box">');
      d.warnings.forEach(function (w) { h.push('<div>⚠ ' + esc(w) + '</div>'); });
      h.push('</div>');
    }

    h.push('<div class="p13-sec">房间</div>');
    h.push('<div class="p13-row"><span>状态</span><span class="' +
      (d.running ? 'p13-warn' : 'p13-ok') + '">' +
      (d.running ? '跑着' : '空闲') + '</span></div>');
    h.push('<div class="p13-row"><span>总轮次</span><span>' + d.turns + '</span></div>');
    h.push('<div class="p13-row"><span>数据目录</span><span class="p13-cfg">' + esc(d.room) + '</span></div>');

    h.push('<div class="p13-sec">Agents</div>');
    (d.agents || []).forEach(function (a) {
      var conn = a.connection === '在线'
        ? '<span class="p13-ok">在线</span>'
        : '<span class="p13-err">' + esc(a.connection) + '</span>';
      var cfg = Object.keys(a.config || {}).map(function (k) {
        return k + '=' + a.config[k];
      }).join(' · ');
      // 把值得注意的配置标出来
      var flags = [];
      if (a.config && a.config.mode === 'plan') flags.push('只读');
      if (a.config && a.config.thinking === 'max') flags.push('推理拉满');
      if (a.permission === 'allow') flags.push('全自动放行');
      h.push('<div class="p13-agent">');
      h.push('<div class="p13-row"><span style="color:' + esc(a.color || '#888') +
        ';font-weight:600">' + esc(a.name) + '</span><span>' + conn +
        (a.busy ? ' <span class="p13-warn">发言中</span>' : '') + '</span></div>');
      h.push('<div class="p13-cfg">' + esc(cfg) + '</div>');
      h.push('<div class="p13-row"><span>轮次 · 中位 · 最慢</span><span>' +
        a.turns + ' · ' + sec(a.p50_ms) +
        (a.max_ms > 120000 ? ' · <span class="p13-warn">' + sec(a.max_ms) + '</span>'
                           : ' · ' + sec(a.max_ms)) + '</span></div>');
      h.push('<div class="p13-row"><span>累计 token</span><span>' + ktok(a.tokens_total) +
        (a.errors ? ' · <span class="p13-err">' + a.errors + ' 错</span>' : '') + '</span></div>');
      if (flags.length) {
        h.push('<div class="p13-row"><span>注意</span><span class="p13-warn">' +
          esc(flags.join(' · ')) + '</span></div>');
      }
      h.push('</div>');
    });

    h.push('<div class="p13-sec">讨论</div>');
    var dd = d.discussion || {};
    h.push('<div class="p13-row"><span>讨论法</span><span>' + esc(dd.method || '—') + '</span></div>');
    h.push('<div class="p13-row"><span>交锋轮上限</span><span>' + (dd.max_rounds || 0) + '</span></div>');
    h.push('<div class="p13-row"><span>已用 token</span><span>' + ktok(dd.tokens_used) + '</span></div>');

    h.push('<div class="p13-sec">审计</div>');
    var au = d.audit || {};
    if (!au.enabled) {
      h.push('<div class="p13-dim">未接入</div>');
    } else if (au.error) {
      h.push('<div class="p13-err">' + esc(au.error) + '</div>');
    } else {
      h.push('<div class="p13-row"><span>记录数</span><span class="' +
        ((au.total || 0) ? 'p13-ok' : 'p13-warn') + '">' + (au.total || 0) + '</span></div>');
      var dec = au.by_decision || {};
      Object.keys(dec).forEach(function (k) {
        h.push('<div class="p13-row"><span>' + esc(k) + '</span><span>' + dec[k] + '</span></div>');
      });
      if (au.errors) {
        h.push('<div class="p13-row"><span>错误</span><span class="p13-err">' + au.errors + '</span></div>');
      }
    }

    h.push('<div class="p13-sec">落盘</div>');
    var f = d.files || {};
    Object.keys(f).forEach(function (k) {
      h.push('<div class="p13-row"><span>' + esc(k) + '</span><span>' + bytes(f[k]) + '</span></div>');
    });

    body.innerHTML = h.join('');
  }

  function placeholder(msg) {
    var body = document.getElementById('p13-body');
    if (body) body.innerHTML = '<div class="p13-ph">' + esc(msg) + '</div>';
  }

  function refresh() {
    fetch(API).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(render).catch(function (e) {
      placeholder('健康接口不可用：' + e.message +
        '\n（后端未重启，或 /api/health 未实现）\n页面其余功能不受影响。');
    });
  }

  function boot() {
    injectStyle();
    if (!build()) return;
    setCollapsed(collapsed);
    refresh();
    setInterval(refresh, POLL);
  }

  window.P13 = { refresh: refresh, setCollapsed: setCollapsed };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
