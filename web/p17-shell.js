/* P17 外壳 —— 管版面，不管内容。
 *
 * ## 为什么重写（2026-11-04 第二次）
 *
 * 第一版把 P1 配置 / P3 技能 / P16 算力三块折成 <details> 塞在侧栏里，
 * 想靠"默认收起"省空间。实测的结果是：
 *
 *   header 78 + agents 509 + p14 42 + 3×折叠区 45 + actions 168 = 887px
 *
 * 侧栏可用高度 950px，看着够，但只要展开任意一块就溢出；而 `.aside-actions`
 * 是 `position: sticky; bottom: 0`，于是它**盖住**了展开的面板（实测 p16
 * 算力表 383px 被压在按钮底下，看不见也点不到）。
 *
 * 更根本的问题：侧栏同时承担了两种职责——"谁在这个房间里"（agent 列表）
 * 和"调设置"（模型/技能/算力）。这两件事的使用频率差一个数量级，
 * 放在同一列里必然互相挤。
 *
 * ## 现在的结构
 *
 *   侧栏 300px          主区                    右侧抽屉 340px（按需）
 *   ┌──────────┐   ┌──────────────────┐   ┌──────────────┐
 *   │ 房间标题  │   │ 消息流（滚动）    │   │ 配置│技能│算力│
 *   │ agent 列表│   │                  │   │              │
 *   │ （自己滚）│   ├──────────────────┤   │  对应面板     │
 *   │ +加 agent │   │ 输入区 + 控件     │   │              │
 *   │ 动作按钮  │   └──────────────────┘   └──────────────┘
 *   └──────────┘
 *
 * 侧栏从此**永不滚动**（agent 列表自己滚），动作区也不再 sticky——
 * 因为它现在真的在底部，不需要靠 sticky 抢位置。
 *
 * ## 为什么不改各面板的 JS
 *
 * P1/P3/P16 是不同轮次交付的，各自有挂载方式和持久化。这里只做一件事：
 * 面板建好之后把它们的 DOM **搬进**抽屉。appendChild 不破坏它们的引用，
 * 所以面板内部一行都不用改，仍能独立演进。
 */
(function () {
  'use strict';

  var RAIL_KEY = 'shell-rail-v1';
  var PANEL_KEY = 'shell-rail-tab-v1';

  // 右侧的 P7 活动 / P13 健康是各自独立的浮层，仍然是互斥关系；
  // 抽屉也算一个"右栏"，三者只能开一个，否则叠在一起。
  var OVERLAYS = [
    { key: 'p7', cls: 'p7-rail-open', close: '#p7-audit .p7-aud-close', tab: '#p7-aud-tab' },
    { key: 'p13', cls: 'p13-rail-open', close: '#p13-close', tab: '#p13-tab' }
  ];

  var TABS = [
    { key: 'cfg',   label: '配置', sel: '.p1-config' },
    { key: 'skill', label: '技能', sel: '#p3-skills' },
    { key: 'perf',  label: '算力', sel: '#p16-perf' }
  ];

  var lastOverlay = null;
  var rail = null;
  var activeTab = 'cfg';

  function $(s) { return document.querySelector(s); }

  /* ---------- 抽屉本体 ---------- */

  function buildRail() {
    if (rail) return rail;
    rail = document.createElement('aside');
    rail.id = 'rail';
    rail.className = 'rail';
    rail.innerHTML =
      '<div class="rail-head">' +
        '<div class="rail-tabs">' +
          TABS.map(function (t) {
            return '<button class="rail-tab" data-tab="' + t.key + '">' + t.label + '</button>';
          }).join('') +
        '</div>' +
        '<button class="rail-close" id="rail-close" title="收起（Esc）">✕</button>' +
      '</div>' +
      '<div class="rail-body">' +
        TABS.map(function (t) {
          return '<div class="rail-pane" data-pane="' + t.key + '"></div>';
        }).join('') +
      '</div>';

    document.body.appendChild(rail);

    rail.querySelectorAll('.rail-tab').forEach(function (b) {
      b.addEventListener('click', function () { showTab(b.dataset.tab); });
    });
    rail.querySelector('#rail-close').addEventListener('click', function () { setRail(false); });

    return rail;
  }

  function pane(key) {
    return rail && rail.querySelector('[data-pane="' + key + '"]');
  }

  function showTab(key) {
    if (!pane(key)) key = TABS[0].key;
    activeTab = key;
    rail.querySelectorAll('.rail-tab').forEach(function (b) {
      b.classList.toggle('on', b.dataset.tab === key);
    });
    rail.querySelectorAll('.rail-pane').forEach(function (p) {
      p.classList.toggle('on', p.dataset.pane === key);
    });
    try { localStorage.setItem(PANEL_KEY, key); } catch (e) { /* 隐私模式 */ }
    // 面板可能在被隐藏时渲染过（宽度 0），切回来重新量一次。
    window.dispatchEvent(new Event('resize'));
  }

  function isRailOpen() {
    return document.body.classList.contains('rail-open');
  }

  function setRail(on) {
    buildRail();
    document.body.classList.toggle('rail-open', !!on);
    try { localStorage.setItem(RAIL_KEY, on ? '1' : '0'); } catch (e) { /* ignore */ }
    if (on) {
      // 抽屉和 P7/P13 浮层互斥：它们都贴右边缘，一起开必然重叠
      closeOverlays();
      showTab(activeTab);
    }
  }

  function closeOverlays() {
    OVERLAYS.forEach(function (r) {
      if (document.body.classList.contains(r.cls)) {
        var btn = $(r.close);
        if (btn) btn.click();
      }
    });
  }

  /* ---------- 把面板搬进抽屉 ----------
   * P1 在 boot 时建 section、P16 建 section 后插进 aside、P3 需要 #p3-skills
   * 已存在。三者时机不确定，所以：先试一次，再用 MutationObserver 兜后续的。 */

  function moveInto(sel, tabKey) {
    var el = document.querySelector(sel);
    var target = pane(tabKey);
    if (!el || !target) return false;
    if (el.parentElement === target) return true;   // 已经在里面
    target.appendChild(el);
    return true;
  }

  function collect() {
    buildRail();
    var moved = 0;
    TABS.forEach(function (t) { if (moveInto(t.sel, t.key)) moved++; });
    // 空分页给个占位，免得点进去是一片空白（看着像坏了）
    TABS.forEach(function (t) {
      var p = pane(t.key);
      if (p && !p.children.length && !p.querySelector('.rail-empty')) {
        var d = document.createElement('div');
        d.className = 'rail-empty';
        d.textContent = '这一块还没加载出来。';
        p.appendChild(d);
      }
    });
    expandSkillsOnce();
    return moved;
  }

  /* P3 技能面板默认收起（`p3-open` 没设过就是关的），这是它在窄侧栏里
   * 为了不挤掉 agent 列表做的妥协。搬进整屏抽屉之后没有挤的问题了，
   * 一个 40px 的空标题栏反而像坏了。
   * 只在用户从没手动设过开关时展开一次；设过就尊重人家。 */
  function expandSkillsOnce() {
    var p = document.getElementById('p3-skills');
    if (!p || !p.classList.contains('p3-closed')) return;
    var seen = null;
    try { seen = localStorage.getItem('p3-open'); } catch (e) { return; }
    if (seen !== null) return;
    var tg = document.getElementById('p3-toggle');
    if (tg) tg.click();
  }

  /* ---------- 原有职责：讨论控件的去重 ---------- */

  function hideDuplicateDiscussControls() {
    var panel = document.getElementById('p2-panel');
    if (!panel) return false;
    var row = panel.querySelector('.p2-row');
    if (!row) return false;
    row.style.display = 'none';
    return true;
  }

  var CHAT_ONLY = ['rounds', 'auto'];

  function syncChatOnlyControls() {
    var discuss = !!(window.P12 && window.P12.mode && window.P12.mode() === 'discuss');
    CHAT_ONLY.forEach(function (id) {
      var el = document.getElementById(id);
      var label = el && el.closest && el.closest('label');
      if (label) label.style.display = discuss ? 'none' : '';
    });
  }

  /* ---------- 浮层互斥（P7 / P13 仍然是浮层） ---------- */

  function enforceSingleOverlay() {
    var open = OVERLAYS.filter(function (r) {
      return document.body.classList.contains(r.cls);
    });
    if (open.length >= 2) {
      var keep = lastOverlay && open.some(function (r) { return r.key === lastOverlay; })
        ? lastOverlay : open[open.length - 1].key;
      open.forEach(function (r) {
        if (r.key === keep) return;
        var btn = $(r.close);
        if (btn) btn.click();
      });
    }
    // 有浮层开着就把抽屉收起来，否则两个 340px 的东西叠在右边
    if (open.length && isRailOpen()) setRail(false);
  }

  function watchOverlays() {
    OVERLAYS.forEach(function (r) {
      var tab = $(r.tab);
      if (tab) tab.addEventListener('click', function () { lastOverlay = r.key; }, true);
    });
    new MutationObserver(function () {
      enforceSingleOverlay();
      if (!OVERLAYS.some(function (r) { return document.body.classList.contains(r.cls); })) {
        if (document.body.style.paddingRight) document.body.style.paddingRight = '';
      }
    }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
  }

  /* ---------- 侧栏：清掉旧版的 <details> 折叠 ---------- */

  function flattenSidebar() {
    // 旧版把面板包进 <details class="shell-sec">。新版面板已经搬去抽屉，
    // 这些壳子会剩下空的摘要行，一并去掉。
    document.querySelectorAll('aside .shell-sec').forEach(function (d) {
      if (!d.querySelector('.p1-config, #p3-skills, #p16-perf')) d.remove();
    });
    // 动作区曾经靠内联样式，且在旧版里是 sticky
    var acts = document.querySelector('aside > div.aside-actions');
    if (acts) { acts.style.position = ''; acts.style.bottom = ''; }
  }

  /* ---------- 启动 ---------- */

  function boot() {
    buildRail();
    try {
      activeTab = localStorage.getItem(PANEL_KEY) || 'cfg';
      if (localStorage.getItem(RAIL_KEY) === '1') document.body.classList.add('rail-open');
    } catch (e) { /* 隐私模式 */ }

    var btn = document.getElementById('rail-btn');
    if (btn) btn.addEventListener('click', function () { setRail(!isRailOpen()); });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isRailOpen()) setRail(false);
    });

    // 面板是异步挂载的：先立刻试，再用 observer 兜住后建的。
    // 观察整个 body（而不是 aside）：面板搬进抽屉之后，它们的后续 DOM
    // 变化发生在抽屉里，只盯 aside 会漏掉（实测 p3 的展开按钮就是这么漏的）。
    collect();
    var mo = new MutationObserver(function () { collect(); });
    mo.observe(document.body, { childList: true, subtree: true });
    // 3 秒后停掉观察，避免长期开销（面板不会一直建）
    setTimeout(function () { mo.disconnect(); collect(); }, 3000);
    // 异步面板有几次明显的到达时间，额外补几次，确保 p3 展开判定能命中
    setTimeout(expandSkillsOnce, 900);
    setTimeout(expandSkillsOnce, 1800);
    setTimeout(expandSkillsOnce, 3000);

    watchOverlays();
    showTab(activeTab);
    flattenSidebar();

    retry(hideDuplicateDiscussControls, 8);
    syncChatOnlyControls();
    ['p12-chat', 'p12-disc'].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.addEventListener('click', function () { setTimeout(syncChatOnlyControls, 0); });
    });
  }

  function retry(fn, tries) {
    if (fn()) return;
    var n = (tries || 6) - 1;
    if (n > 0) setTimeout(function () { retry(fn, n); }, 300);
  }

  // G4：给侧栏一个入口，直接把抽屉开到某个标签页（点模型名 → 配置页）。
  // 外壳的其它交互都还是自己管，这里只是把 setRail + showTab 拼一下。
  window.P17Shell = {
    openPanel: function (key) {
      setRail(true);
      showTab(key);
      collect();          // 面板可能还没搬进来（p1-config 是异步建的）
    },
    isOpen: isRailOpen
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
