/* P3 技能加载 —— 前端面板（codex 交付）
 *
 * 只新增文件，不改 index.html / app.js。两种用法：
 *   1) pi 在 index.html 里加一行 <script src="/static/p3-skills.js"> 和一个
 *      <div id="p3-skills"></div>，本文件会自动挂载；
 *   2) 或者在控制台调 P3Skills.mount(document.body) 手动挂。
 *
 * 数据全部来自现有接口 GET /api/skills（server.py 已实现，未改动）：
 *   {"catalog": [...], "broken": N}
 * 每条 catalog entry 现在带 conflicts 数组（P3 新增字段），所以前端能直接显示
 * 谁赢了、谁被丢了，不需要新接口。
 *
 * ⚠ 已知限制：prompt 预览是**客户端镜像** core/skills.py 的 _assemble 逻辑。
 * 两边各改一处就会漂移。要根治，pi 得加一个 GET /api/skills/prompt?...
 * （见交付说明"需要 pi 改什么"第 2 条）。
 */
(function (global) {
  'use strict';

  var API = '/api/skills';
  var VERSION = 'p3-skills.js@2';
  var state = { data: null, opts: { mode: 'full', max_items: 20, desc_limit: 100,
                                     budget: 1200, list_all_names: true } };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    return n < 1024 ? n + 'B' : (n / 1024).toFixed(1) + 'KB';
  }

  function shortPath(p) {
    var s = String(p || '').replace(/\\/g, '/');
    var m = s.match(/(?:^|\/)(\.claude|\.agents|\.pi|\.codex|AppData)[^\/]*/);
    return s.length > 46 ? '…' + s.slice(s.length - 44) : s;
  }

  /* ---------- 与 core/skills.py 对齐的截断逻辑 ---------- */

  function clip(text, limit) {
    var one = String(text || '').split(/\s+/).join(' ');
    if (!limit || limit <= 0) return '';
    if (one.length <= limit) return one;
    return one.slice(0, limit).replace(/\s+$/, '') + '…';
  }

  function tokens(text) {
    var out = {};
    String(text || '').toLowerCase()
      .split(/[^0-9a-z\u4e00-\u9fff]+/)
      .forEach(function (piece) {
        if (!piece) return;
        out[piece] = 1;
        var cjk = piece.match(/[\u4e00-\u9fff]/g);
        if (cjk) cjk.forEach(function (c) { out[c] = 1; });
      });
    return Object.keys(out);
  }

  function ranked(items, query) {
    var q = tokens(query || '');
    if (!q.length) {
      return items.slice().sort(function (a, b) {
        return (a.root_index - b.root_index) || (a.name < b.name ? -1 : 1);
      });
    }
    function score(s) {
      var nt = tokens(s.name), dt = tokens(s.description || '');
      var n = nt.filter(function (t) { return q.indexOf(t) >= 0; }).length;
      var d = dt.filter(function (t) { return q.indexOf(t) >= 0; }).length;
      return [-(3 * n + d), s.root_index || 0, s.name];
    }
    return items.slice().sort(function (a, b) {
      var sa = score(a), sb = score(b);
      for (var i = 0; i < 3; i++) {
        if (sa[i] !== sb[i]) return sa[i] < sb[i] ? -1 : 1;
      }
      return 0;
    });
  }

  /* 镜像 skills.py 的 _assemble：budget 是总长上限，低于"全列名字"下界时照发并告警 */
  function buildPrompt(items, opts) {
    var order = ranked(items, opts.query || '');
    var header = '[可用技能 · 共 ' + order.length + ' 个]';
    var footer = '要用某个技能，在你回复里写 [USE_SKILL: 名字]，我会把正文给你。';
    var names = order.map(function (s) { return s.name; }).join(', ');
    var dropLine = opts.list_all_names
      ? '其余 ' + order.length + ' 个（只给名字）：' + names
      : '（还有 ' + order.length + ' 个未列出，可用 [USE_SKILL: 名字] 点名）';
    var floor = order.length
      ? [header, dropLine, footer].join('\n')
      : [header, footer].join('\n');
    if (opts.budget > 0 && floor.length > opts.budget) {
      return { text: floor, warn: '预算 ' + opts.budget + ' 字符低于"全列名字"下界 ' +
        floor.length + '，只能照发', over: true };
    }
    var limit = opts.mode === 'none' ? 0
      : (opts.mode === 'short' ? 60 : opts.desc_limit);
    var base = header.length + footer.length + 1;
    var avail = opts.budget > 0 ? opts.budget - base - dropLine.length : null;
    var shown = [], used = 0;
    order.forEach(function (s) {
      var line = limit === 0 ? '- ' + s.name
        : '- ' + s.name + ': ' + clip(s.description, limit);
      var cost = line.length + 1;
      if (shown.length >= opts.max_items || (avail !== null && used + cost > avail)) return;
      shown.push(line); used += cost;
    });
    var dropped = order.length - shown.length;
    var realDrop = dropped > 0
      ? (opts.list_all_names
          ? '其余 ' + dropped + ' 个（只给名字）：' + order.slice(shown.length).map(function (s) { return s.name; }).join(', ')
          : '（还有 ' + dropped + ' 个未列出，可用 [USE_SKILL: 名字] 点名）')
      : [];
    var text = [header].concat(shown).concat(realDrop).concat([footer]).join('\n');
    return { text: text, over: opts.budget > 0 && text.length > opts.budget,
             warned: false };
  }

  /* ---------- 渲染 ---------- */

  function overview(cat, broken) {
    var uniq = cat.length;
    var withDesc = cat.filter(function (s) { return !!(s.description || '').trim(); }).length;
    var conflicted = cat.filter(function (s) { return s.conflicts && s.conflicts.length; }).length;
    var dropped = cat.reduce(function (n, s) { return n + (s.conflicts ? s.conflicts.length : 0); }, 0);
    return [
      '<div class="p3-ov">',
      '  <span class="p3-pill">唯一 <b>' + uniq + '</b></span>',
      '  <span class="p3-pill">冲突 <b class="' + (conflicted ? 'p3-bad' : '') + '">'
        + conflicted + '</b> 组 / 丢 ' + dropped + ' 份</span>',
      '  <span class="p3-pill">缺 description <b class="' + (uniq - withDesc ? 'p3-bad' : '') + '">'
        + (uniq - withDesc) + '</b></span>',
      '  <span class="p3-pill">解析失败 <b class="' + (broken ? 'p3-bad' : '') + '">'
        + broken + '</b></span>',
      '</div>'
    ].join('');
  }

  function conflictBlock(cat) {
    var hits = cat.filter(function (s) { return s.conflicts && s.conflicts.length; });
    if (!hits.length) return '<div class="p3-empty">无冲突</div>';
    return '<table class="p3-conf"><thead><tr><th>技能</th><th>胜者</th><th>被丢弃</th></tr></thead><tbody>' +
      hits.map(function (s) {
        return '<tr><td>' + esc(s.name) + '</td><td><code>' + esc(shortPath(s.path)) +
          '</code> <span class="p3-dim">' + esc(shortPath(s.source)) + '</span></td><td>' +
          s.conflicts.map(function (c) {
            return '<div><code>' + esc(shortPath(c.path)) + '</code> ' +
              '<span class="p3-dim">' + fmtBytes(c.bytes) + '</span></div>';
          }).join('') + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  function table(cat) {
    return '<table class="p3-tab"><thead><tr><th>状态</th><th>name</th><th>bytes</th>' +
      '<th>source</th><th>description</th></tr></thead><tbody>' +
      cat.map(function (s) {
        var st = (s.conflicts && s.conflicts.length) ? 'DUP' : 'OK';
        return '<tr><td class="p3-' + st.toLowerCase() + '">' + st + '</td>' +
          '<td><code>' + esc(s.name) + '</code></td>' +
          '<td>' + fmtBytes(s.bytes) + '</td>' +
          '<td class="p3-dim">' + esc(shortPath(s.source)) + '</td>' +
          '<td>' + esc(clip(s.description, 160)) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  function preview(cat) {
    var o = state.opts;
    var res = buildPrompt(cat, o);
    return [
      '<div class="p3-ctl">',
      '  <label>模式 <select id="p3-mode">',
      '    <option value="full"' + (o.mode === 'full' ? ' selected' : '') + '>full（name + description）</option>',
      '    <option value="short"' + (o.mode === 'short' ? ' selected' : '') + '>short（前 60 字）</option>',
      '    <option value="none"' + (o.mode === 'none' ? ' selected' : '') + '>none（只列 name）</option>',
      '  </select></label>',
      '  <label>max_items <input type="number" id="p3-max" min="0" max="200" value="' + o.max_items + '"></label>',
      '  <label>desc_limit <input type="number" id="p3-desc" min="0" max="800" value="' + o.desc_limit + '"></label>',
      '  <label>budget <input type="number" id="p3-budget" min="0" max="20000" value="' + o.budget + '"></label>',
      '  <label><input type="checkbox" id="p3-names"' + (o.list_all_names ? ' checked' : '') +
        '> 被砍的仍列名字</label>',
      '  <button id="p3-copy" title="复制到剪贴板">复制清单</button>',
      '</div>',
      '<div class="p3-meta">prompt <b>' + res.text.length + '</b> 字符 / 预算 ' + o.budget +
        ' <span class="p3-' + (res.over ? 'bad' : 'ok') + '">' +
        (res.over ? '⚠ 超预算（已在服务端告警口径内）' : '未超') + '</span>',
        res.warn ? '<div class="p3-warn">' + esc(res.warn) + '</div>' : '',
      '</div>',
      '<textarea id="p3-prompt" readonly rows="14">' + esc(res.text) + '</textarea>'
    ].join('');
  }

  var open = false;
  try { open = localStorage.getItem('p3-open') === '1'; } catch (e) { /* ignore */ }

  function setOpen(on, persist) {
    open = on;
    // persist === false 用于"只是把当前状态画上去"。渲染不该写偏好：
    // 原来 paint() 每次都调 setOpen(open)，于是首屏渲染就把 p3-open 落盘，
    // 用户"从没点过这个开关"这件事被抹掉了，外面就没法判断要不要给默认值。
    if (persist !== false) {
      try { localStorage.setItem('p3-open', on ? '1' : '0'); } catch (e) { /* ignore */ }
    }
    var p = document.getElementById('p3-skills');
    if (p) p.classList.toggle('p3-closed', !on);
    var b = document.getElementById('p3-toggle');
    if (b) b.textContent = on ? '▾' : '▸';
  }

  function paint(el) {
    var cat = (state.data && state.data.catalog) || [];
    var broken = (state.data && state.data.broken) || 0;
    var conflicted = cat.filter(function (s) { return s.conflicts && s.conflicts.length; }).length;
    el.innerHTML =
      '<div class="p3-head"><h3>技能加载 <span class="p3-dim">' + cat.length + ' 个' +
      (conflicted ? ' · 冲突 ' + conflicted : '') + (broken ? ' · 失败 ' + broken : '') +
      '</span></h3>' +
      '<span class="p3-hact"><button id="p3-toggle" title="展开 / 收起">' + (open ? '▾' : '▸') +
      '</button><button id="p3-refresh">刷新</button></span></div>' +
      '<div class="p3-body">' +
      overview(cat, broken) +
      '<section><h4>同名冲突（duplicate_policy = first）</h4>' + conflictBlock(cat) + '</section>' +
      '<section><h4>生效目录 <span class="p3-dim">' + cat.length + ' 个</span></h4>' +
        (cat.length ? table(cat) : '<div class="p3-empty">扫描不到技能</div>') + '</section>' +
      '<section><h4>清单预览（每轮进 prompt 的那段）</h4>' + preview(cat) + '</section>' +
      '<div class="p3-foot">' + esc(VERSION) + ' · 数据源 <code>GET ' + esc(API) + '</code> · ' +
        '预览是客户端镜像，服务端口径见 core/skills.py 的 _assemble</div>' +
      '</div>';
    setOpen(open, false);   // 只画状态，不写偏好

    function bind(id, fn) {
      var n = document.getElementById(id);
      if (n) n.addEventListener('input', fn);
    }
    bind('p3-mode', function (e) { state.opts.mode = e.target.value; paint(el); });
    bind('p3-max', function (e) { state.opts.max_items = parseInt(e.target.value, 10) || 0; paint(el); });
    bind('p3-desc', function (e) { state.opts.desc_limit = parseInt(e.target.value, 10) || 0; paint(el); });
    bind('p3-budget', function (e) { state.opts.budget = parseInt(e.target.value, 10) || 0; paint(el); });
    bind('p3-names', function (e) { state.opts.list_all_names = e.target.checked; paint(el); });
    var cp = document.getElementById('p3-copy');
    if (cp) cp.addEventListener('click', function () {
      var ta = document.getElementById('p3-prompt');
      if (!ta) return;
      ta.select();
      try { document.execCommand('copy'); cp.textContent = '已复制'; }
      catch (e) { cp.textContent = '复制失败'; }
      setTimeout(function () { cp.textContent = '复制清单'; }, 1200);
    });
    var rf = document.getElementById('p3-refresh');
    if (rf) rf.addEventListener('click', function () { refresh(el); });
    var tg = document.getElementById('p3-toggle');
    if (tg) tg.addEventListener('click', function () { setOpen(!open); });
  }

  function refresh(el) {
    return fetch(API, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) { state.data = d; paint(el); return d; })
      .catch(function (e) {
        el.innerHTML = '<div class="p3-head"><h3>技能加载</h3></div>' +
          '<div class="p3-err">取不到 ' + esc(API) + '：' + esc(e.message) + '</div>';
        throw e;
      });
  }

  /* ---------- 样式（本文件自带；此前完全没有 CSS，表格 718px 宽直接压到对话区） ---------- */
  var CSS = [
    /* 面板本身：侧栏里的独立滚动块，高度有上限，不再把整个 aside 撑到 4000px */
    '.p3-panel{flex:0 0 auto;max-height:34vh;min-height:0;overflow:auto;overscroll-behavior:contain;',
    '  border-top:1px solid var(--line);padding:8px 10px 10px;font-size:11px;line-height:1.45}',
    '.p3-head{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;',
    '  gap:8px;background:var(--panel);padding:2px 0 7px;margin-top:-8px;border-bottom:1px solid var(--line)}',
    '.p3-head h3{margin:0;font-size:12px;font-weight:600}',
    '.p3-hact{display:flex;gap:5px;flex:none}',
    '.p3-head button{padding:2px 9px;font-size:10px;border-radius:6px;line-height:1.5}',
    '#p3-toggle{padding:2px 6px;min-width:22px}',
    /* 收起状态：只剩一行标题，侧栏能装下 agent 列表 */
    '.p3-panel.p3-closed{max-height:none;padding-bottom:8px}',
    '.p3-panel.p3-closed .p3-body{display:none}',
    '.p3-ov{display:flex;flex-wrap:wrap;gap:4px 6px;margin:8px 0 2px}',
    '.p3-pill{font-size:10px;color:var(--dim);border:1px solid var(--line);border-radius:999px;',
    '  padding:1px 7px;white-space:nowrap}',
    '.p3-pill b{color:var(--text);font-weight:600}',
    '.p3-bad{color:var(--err)}',
    '.p3-ok{color:var(--ok)}',
    '.p3-warn{color:var(--warn);font-size:10px;margin-top:4px}',
    '.p3-dim{color:var(--dim);font-weight:400}',
    '.p3-empty{color:var(--dim);padding:6px 0}',
    '.p3-err{color:var(--err);padding:6px 0}',
    '.p3-panel section{margin-top:12px}',
    '.p3-panel h4{margin:0 0 5px;font-size:11px;font-weight:600;color:var(--dim)}',
    /* 表格：定宽分栏 + 任意断行 → 永远不出容器 */
    '.p3-panel table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:10px;line-height:1.4}',
    '.p3-panel th,.p3-panel td{padding:3px;border-bottom:1px solid var(--line);vertical-align:top;',
    '  text-align:left;overflow-wrap:anywhere;word-break:break-word}',
    '.p3-panel th{color:var(--dim);font-weight:500}',
    '.p3-panel td code{font-family:Consolas,"Cascadia Mono",monospace;font-size:9.5px;',
    '  color:var(--accent);word-break:break-all}',
    /* 5 列塞不进 260px：source 列在冲突表里已经有了，这里收起来 */
    '.p3-tab th:nth-child(1),.p3-tab td:nth-child(1){width:17%}',
    '.p3-tab th:nth-child(2),.p3-tab td:nth-child(2){width:33%}',
    '.p3-tab th:nth-child(3),.p3-tab td:nth-child(3){width:16%}',
    '.p3-tab th:nth-child(4),.p3-tab td:nth-child(4){display:none}',
    '.p3-conf th:nth-child(1),.p3-conf td:nth-child(1){width:26%}',
    '.p3-conf th:nth-child(2),.p3-conf td:nth-child(2){width:38%}',
    '.p3-conf th:nth-child(3),.p3-conf td:nth-child(3){width:36%}',
    '.p3-dup{color:var(--err)}',
    /* 控件 */
    '.p3-ctl{display:flex;flex-wrap:wrap;gap:5px 8px;align-items:center;font-size:10px;color:var(--dim)}',
    '.p3-ctl label{display:flex;gap:4px;align-items:center;white-space:nowrap}',
    '.p3-ctl select,.p3-ctl input{background:var(--bg);color:var(--text);border:1px solid var(--line);',
    '  border-radius:5px;padding:1px 4px;font:inherit;font-size:10px}',
    '.p3-ctl input[type=number]{width:52px}',
    '.p3-ctl button{padding:1px 8px;font-size:10px;border-radius:5px}',
    '.p3-meta{margin-top:6px;font-size:10px;color:var(--dim)}',
    '.p3-meta b{color:var(--text)}',
    '.p3-panel textarea#p3-prompt{width:100%;height:120px;margin-top:6px;resize:vertical;',
    '  background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;',
    '  padding:6px 8px;font-family:Consolas,"Cascadia Mono",monospace;font-size:9.5px;line-height:1.45}',
    '.p3-foot{margin-top:10px;padding-top:6px;border-top:1px solid var(--line);',
    '  font-size:10px;color:var(--dim);word-break:break-all}'
  ].join('');

  function injectStyle() {
    if (document.getElementById('p3-style')) return;
    var st = document.createElement('style');
    st.id = 'p3-style';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  function mount(el) {
    var target = el || document.getElementById('p3-skills');
    if (!target) return null;
    el = target;
    injectStyle();
    el.classList.add('p3-panel');
    refresh(el).catch(function () {});
    return el;
  }

  global.P3Skills = { mount: mount, refresh: refresh, paint: paint,
                      buildPrompt: buildPrompt, clip: clip, VERSION: VERSION };

  if (typeof document !== 'undefined' && document.getElementById('p3-skills')) {
    mount();
  }
})(window);