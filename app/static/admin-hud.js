/* ============================================================================
   OCS AI Gateway · Admin HUD runtime
   ----------------------------------------------------------------------------
   只放“新设计层”需要的东西，避免继续膨胀 admin.html 里的内联脚本：

   - palette()      从 CSS 变量读当前主题的实际颜色，给 ECharts / SVG 用；
   - syncOverview() 把旧接口写入的数值映射成 HUD 状态（绿点/黄点/红点）；
   - onThemeChange() 同步浏览器地址栏 theme-color。

   依赖：admin-hud.css 必须先加载。
   ========================================================================== */
(function () {
  'use strict';

  function cssVar(name, fallback) {
    try {
      var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return value || fallback;
    } catch (e) {
      return fallback;
    }
  }

  function palette() {
    return {
      bg: cssVar('--hud-bg', '#282828'),
      card: cssVar('--hud-card', 'rgba(255,255,255,.05)'),
      popover: cssVar('--hud-popover', '#303030'),
      fg: cssVar('--hud-fg', '#f5f5f7'),
      fg2: cssVar('--hud-fg-2', '#98989d'),
      fg3: cssVar('--hud-fg-3', '#6e6e73'),
      border: cssVar('--hud-border-soft', 'rgba(255,255,255,.07)'),
      borderStrong: cssVar('--hud-border', 'rgba(255,255,255,.12)'),
      primary: cssVar('--hud-primary', '#0a84ff'),
      primarySoft: cssVar('--hud-primary-soft', 'rgba(10,132,255,.14)'),
      ok: cssVar('--hud-ok', '#3ddc84'),
      warn: cssVar('--hud-warn', '#ffd23f'),
      danger: cssVar('--hud-danger', '#ff453a'),
      purple: cssVar('--hud-purple', '#c084fc')
    };
  }

  function num(id) {
    var el = document.getElementById(id);
    if (!el) return 0;
    var match = String(el.textContent || '').match(/-?\d+/);
    return match ? parseInt(match[0], 10) || 0 : 0;
  }

  /**
   * 把旧接口的数值同步到总览状态。
   * @param {{enabled?: number, total?: number}} [info]
   */
  function syncOverview(info) {
    info = info || {};

    var pill = document.getElementById('hudStatus');
    if (pill && typeof info.enabled === 'number') {
      var enabled = info.enabled;
      var total = typeof info.total === 'number' ? info.total : enabled;
      pill.classList.toggle('on', enabled > 0);
      pill.classList.toggle('off', enabled === 0);
      var label = pill.querySelector('span');
      if (label) {
        label.textContent = enabled > 0
          ? '运行中 · ' + enabled + '/' + total + ' 个 AI'
          : '没有启用的 AI';
      }
    }

    var rows = document.querySelectorAll('[data-count-id]');
    var issues = 0;
    for (var i = 0; i < rows.length; i += 1) {
      var row = rows[i];
      var value = num(row.getAttribute('data-count-id'));
      var level = row.getAttribute('data-level') || 'warn';
      var active = value > 0;
      row.classList.toggle('is-warn', active && level === 'warn');
      row.classList.toggle('is-danger', active && level === 'danger');
      if (active) issues += value;
    }

    var hint = document.getElementById('hudHealthHint');
    if (hint) hint.textContent = issues > 0 ? issues + ' 项待处理' : '一切正常';
  }

  function onThemeChange(theme) {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'light' ? '#f5f5f7' : '#282828');
  }

  /* ------------------------------------------------------------ 导航工作区 */
  var NAV_OPEN_KEY = 'gw_nav_open';
  var NAV_ALERTS = {
    bank: ['navConflictCount', 'navInspectCount'],
    ops: ['navFailCount', 'navUnitCount'],
    learn: ['navDeadlineCount', 'navAutoCount'],
    connect: [],
    data: []
  };

  function navGroups() {
    return Array.prototype.slice.call(document.querySelectorAll('.nav-group'));
  }

  function groupKeyForSection(section) {
    if (!section) return '';
    var item = document.querySelector('.nav-item[data-section="' + section + '"]');
    var group = item && item.closest ? item.closest('.nav-group') : null;
    return group ? group.getAttribute('data-nav-group') : '';
  }

  function setGroupOpen(group, open) {
    var head = group.querySelector('.nav-group-head');
    var body = group.querySelector('.nav-group-body');
    if (!head || !body) return;
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) body.removeAttribute('hidden');
    else body.setAttribute('hidden', '');
  }

  function storeOpenGroup(key) {
    try {
      if (key) localStorage.setItem(NAV_OPEN_KEY, key);
      else localStorage.removeItem(NAV_OPEN_KEY);
    } catch (e) { /* ignore */ }
  }

  function openNavGroup(section) {
    var key = groupKeyForSection(section);
    if (!key) return;
    navGroups().forEach(function (group) {
      setGroupOpen(group, group.getAttribute('data-nav-group') === key);
    });
    storeOpenGroup(key);
  }

  function toggleNavGroup(group) {
    var key = group.getAttribute('data-nav-group');
    var head = group.querySelector('.nav-group-head');
    var isOpen = head && head.getAttribute('aria-expanded') === 'true';
    if (isOpen) {
      setGroupOpen(group, false);
      storeOpenGroup('');
      return;
    }
    navGroups().forEach(function (other) {
      setGroupOpen(other, other === group);
    });
    storeOpenGroup(key);
  }

  function syncNavDots() {
    Object.keys(NAV_ALERTS).forEach(function (key) {
      var dot = document.querySelector('[data-nav-dot="' + key + '"]');
      if (!dot) return;
      var ids = NAV_ALERTS[key] || [];
      var active = ids.some(function (id) { return num(id) > 0; });
      dot.hidden = !active;
    });
  }

  function initNavGroups() {
    var groups = navGroups();
    if (!groups.length) return;
    groups.forEach(function (group) {
      var head = group.querySelector('.nav-group-head');
      if (head) head.addEventListener('click', function () { toggleNavGroup(group); });
    });
    var stored = '';
    try { stored = localStorage.getItem(NAV_OPEN_KEY) || ''; } catch (e) { /* ignore */ }
    if (stored) {
      var target = document.querySelector('.nav-group[data-nav-group="' + stored + '"]');
      if (target) setGroupOpen(target, true);
    }
    var active = document.querySelector('.nav-item.active[data-section]');
    if (active) openNavGroup(active.getAttribute('data-section'));
    syncNavDots();
    if (window.MutationObserver) {
      var observer = new MutationObserver(syncNavDots);
      Object.keys(NAV_ALERTS).forEach(function (key) {
        (NAV_ALERTS[key] || []).forEach(function (id) {
          var el = document.getElementById(id);
          if (el) observer.observe(el, { childList: true, characterData: true, subtree: true });
        });
      });
    }
  }

  initNavGroups();

  window.OCSHud = {
    palette: palette,
    syncOverview: syncOverview,
    onThemeChange: onThemeChange,
    openNavGroup: openNavGroup
  };
})();
