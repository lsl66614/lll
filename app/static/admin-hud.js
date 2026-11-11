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

  window.OCSHud = {
    palette: palette,
    syncOverview: syncOverview,
    onThemeChange: onThemeChange
  };
})();
