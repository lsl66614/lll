// ==UserScript==
// @name         OCS 学习进度上报（只读）
// @namespace    ocs-ai-gateway
// @version      0.12.0
// @description  只读读取学习通 / 中国大学MOOC 课程页的任务点完成情况，上报到本地 OCS AI 网关「进度监控」。不答题、不提交；目录展开默认关闭。
// @match        *://*.chaoxing.com/*
// @match        *://*.icourse163.org/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    http://127.0.0.1:8000/tools/ocs-progress.meta.js
// @downloadURL  http://127.0.0.1:8000/tools/ocs-progress.user.js
// @run-at       document-start
// @homepageURL  https://github.com/liu620305-jpg/ocs-ai-gateway
// @supportURL   https://github.com/liu620305-jpg/ocs-ai-gateway/issues
// @license      MIT
// ==/UserScript==

(function () {
  'use strict';

  // 页面真实 window：Tampermonkey 在带 @grant 的沙箱里，window 不保证等于页面 window。
  const PAGE = (() => {
    try { return typeof unsafeWindow !== 'undefined' && unsafeWindow ? unsafeWindow : (typeof window !== 'undefined' ? window : {}); }
    catch (e) { return typeof window !== 'undefined' ? window : {}; }
  })();
  const DOC = typeof document !== 'undefined' ? document : null;
  const HAS_GM = typeof GM_xmlhttpRequest === 'function';

  function gmGet(key, fallback) {
    try { return typeof GM_getValue === 'function' ? GM_getValue(key, fallback) : fallback; } catch (e) { return fallback; }
  }
  function gmSet(key, value) {
    try { if (typeof GM_setValue === 'function') GM_setValue(key, value); } catch (e) {}
  }
  function gmDel(key) {
    try { if (typeof GM_deleteValue === 'function') GM_deleteValue(key); } catch (e) {}
  }

  // ======== 配置（GM 存储，脚本更新不会丢） ========
  const CONFIG_KEY = 'ocs_progress_config';
  const DEFAULTS = {
    gateway: 'http://127.0.0.1:8000',
    token: '',
    account: '',
    interval: 30000,
    autoExpand: false,
    debug: false
  };
  function normalizeConfig(cfg) {
    cfg.gateway = String(cfg.gateway || DEFAULTS.gateway).replace(/\/+$/, '');
    cfg.interval = Math.max(10000, Math.min(600000, Number(cfg.interval) || DEFAULTS.interval));
    return cfg;
  }
  const CONFIG = (() => {
    let loaded = {};
    try {
      const raw = gmGet(CONFIG_KEY, '');
      if (raw) loaded = JSON.parse(raw);
    } catch (e) { loaded = {}; }
    return normalizeConfig(Object.assign({}, DEFAULTS, loaded));
  })();
  function saveConfig(patch) {
    Object.assign(CONFIG, patch || {});
    normalizeConfig(CONFIG);
    gmSet(CONFIG_KEY, JSON.stringify(CONFIG));
  }

  const CLIENT_ID = (() => {
    let id = String(gmGet('ocs_progress_client_id', '') || '');
    if (!id) {
      id = 'ocsp_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
      gmSet('ocs_progress_client_id', id);
    }
    return id;
  })();
  function nextSeq() {
    const n = Number(gmGet('ocs_progress_seq', 0) || 0) + 1;
    gmSet('ocs_progress_seq', n);
    return n;
  }

  function probeVersion() {
    try {
      if (typeof GM_info !== 'undefined' && GM_info.script && GM_info.script.version) return GM_info.script.version;
    } catch (e) {}
    return '';
  }

  function toast(message) {
    if (!DOC || !DOC.body) return;
    let el = DOC.getElementById('ocs-progress-toast');
    if (!el) {
      el = DOC.createElement('div');
      el.id = 'ocs-progress-toast';
      el.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#111820;color:#e8ecf3;border:1px solid #2b3444;border-radius:8px;padding:8px 12px;font:13px/1.5 "Microsoft YaHei UI","Segoe UI",sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.35);max-width:60vw;word-break:break-all';
      DOC.body.appendChild(el);
    }
    el.textContent = message;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.remove(), 4000);
  }

  function openSettings() {
    if (!DOC || !DOC.body) return;
    const old = DOC.getElementById('ocs-progress-settings');
    if (old) old.remove();
    const wrap = DOC.createElement('div');
    wrap.id = 'ocs-progress-settings';
    wrap.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px;';
    wrap.innerHTML = `
      <div style="width:min(420px,94vw);background:#0f1218;color:#e8ecf3;border:1px solid #2b3444;border-radius:12px;padding:16px;font:13px/1.55 'Microsoft YaHei UI','Segoe UI',sans-serif;box-shadow:0 24px 60px rgba(0,0,0,.55)">
        <div style="font-size:15px;font-weight:600;margin-bottom:10px">OCS 进度脚本设置</div>
        <label style="display:block;margin:8px 0 2px;color:#9aa6b8">网关地址</label>
        <input data-gw style="box-sizing:border-box;width:100%;background:#151a22;color:#e8ecf3;border:1px solid #2b3444;border-radius:6px;padding:7px 9px" placeholder="http://127.0.0.1:8000">
        <label style="display:block;margin:8px 0 2px;color:#9aa6b8">网关 token（可留空）</label>
        <input data-token type="password" style="box-sizing:border-box;width:100%;background:#151a22;color:#e8ecf3;border:1px solid #2b3444;border-radius:6px;padding:7px 9px">
        <label style="display:block;margin:8px 0 2px;color:#9aa6b8">账号别名（留空自动识别）</label>
        <input data-account style="box-sizing:border-box;width:100%;background:#151a22;color:#e8ecf3;border:1px solid #2b3444;border-radius:6px;padding:7px 9px">
        <label style="display:block;margin:8px 0 2px;color:#9aa6b8">上报间隔（毫秒）</label>
        <input data-interval type="number" min="10000" max="600000" step="1000" style="box-sizing:border-box;width:100%;background:#151a22;color:#e8ecf3;border:1px solid #2b3444;border-radius:6px;padding:7px 9px">
        <label style="display:flex;gap:8px;align-items:center;margin:10px 0"><input data-expand type="checkbox"> 允许自动展开目录（默认关闭，会触发页面点击）</label>
        <label style="display:flex;gap:8px;align-items:center;margin:6px 0"><input data-debug type="checkbox"> 调试日志（控制台输出）</label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
          <button data-clear style="background:#151a22;color:#ffb4b4;border:1px solid #4a2b2b;border-radius:6px;padding:6px 10px;cursor:pointer">清除账号别名</button>
          <button data-cancel style="background:#151a22;color:#e8ecf3;border:1px solid #2b3444;border-radius:6px;padding:6px 10px;cursor:pointer">取消</button>
          <button data-save style="background:#c6f24e;color:#0a0b0e;border:0;border-radius:6px;padding:6px 12px;font-weight:600;cursor:pointer">保存</button>
        </div>
      </div>`;
    DOC.body.appendChild(wrap);
    const q = (sel) => wrap.querySelector(sel);
    q('[data-gw]').value = CONFIG.gateway;
    q('[data-token]').value = CONFIG.token || '';
    q('[data-account]').value = CONFIG.account || '';
    q('[data-interval]').value = String(CONFIG.interval);
    q('[data-expand]').checked = Boolean(CONFIG.autoExpand);
    q('[data-debug]').checked = Boolean(CONFIG.debug);
    q('[data-save]').onclick = () => {
      saveConfig({
        gateway: q('[data-gw]').value.trim(),
        token: q('[data-token]').value.trim(),
        account: q('[data-account]').value.trim(),
        interval: Number(q('[data-interval]').value) || DEFAULTS.interval,
        autoExpand: q('[data-expand]').checked,
        debug: q('[data-debug]').checked
      });
      toast('设置已保存');
      wrap.remove();
    };
    q('[data-cancel]').onclick = () => wrap.remove();
    q('[data-clear]').onclick = () => {
      q('[data-account]').value = '';
      CONFIG.account = '';
      gmDel('ocs_progress_account');
      saveConfig({ account: '' });
      toast('已清除账号别名');
    };
    wrap.addEventListener('click', (e) => { if (e.target === wrap) wrap.remove(); });
  }

  function detectAccount() {
    if (CONFIG.account) return CONFIG.account;
    const el = DOC && DOC.querySelector('.u-ui-name, .userName, .user-name, #userName, .name_Chaoxing, .personalName, .ux-usr .name, .u-usrname, .nickname, .username');
    const text = el && (el.innerText || el.textContent || '').trim();
    if (text) {
      CONFIG.account = text;
      gmSet('ocs_progress_account', text);
      saveConfig({ account: text });
      return text;
    }
    const saved = String(gmGet('ocs_progress_account', '') || '');
    if (saved) {
      CONFIG.account = saved;
      saveConfig({ account: saved });
      return saved;
    }
    let input = '';
    try { input = typeof prompt === 'function' ? prompt('第一次使用，请输入一个账号别名（用于区分不同浏览器）：') : ''; } catch (e) {}
    const name = String(input || '').trim() || '未命名账号';
    CONFIG.account = name;
    gmSet('ocs_progress_account', name);
    saveConfig({ account: name });
    return name;
  }

  (function migrateLegacyStorage() {
    try {
      if (typeof localStorage === 'undefined') return;
      const legacy = localStorage.getItem('ocs_progress_account');
      if (legacy && !CONFIG.account) {
        CONFIG.account = legacy;
        gmSet('ocs_progress_account', legacy);
        saveConfig({ account: legacy });
      }
      localStorage.removeItem('ocs_progress_account');
      Object.keys(localStorage).filter((k) => k.indexOf('ocs_progress_api_recipe::') === 0)
        .forEach((k) => localStorage.removeItem(k));
    } catch (e) {}
  })();

  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('OCS 进度：设置网关/账号', openSettings);
    GM_registerMenuCommand('OCS 进度：立即上报一次', () => report(true));
    GM_registerMenuCommand('OCS 进度：清除账号别名', () => {
      CONFIG.account = '';
      gmDel('ocs_progress_account');
      saveConfig({ account: '' });
      toast('已清除账号别名');
    });
  }

  // ======== 网络层：所有网关请求统一走 GM_xmlhttpRequest ========
  let originalPageFetch = null;
  let lastSuccessAt = 0;
  let lastError = '';
  const NAV = typeof navigator !== 'undefined' ? navigator : null;

  function postJSON(path, payload, opts) {
    opts = opts || {};
    const method = String(opts.method || 'POST').toUpperCase();
    const url = CONFIG.gateway + path;
    const body = payload == null ? null : JSON.stringify(payload);
    const headers = { 'Content-Type': 'application/json', 'X-Client-Id': CLIENT_ID };
    if (CONFIG.token) headers['X-Gateway-Token'] = CONFIG.token;
    const timeout = opts.timeout || 12000;
    return new Promise((resolve, reject) => {
      if (HAS_GM) {
        try {
          const req = {
            method: method,
            url: url,
            headers: headers,
            timeout: timeout,
            onload(res) {
              let data = null;
              try { data = JSON.parse(res.responseText || '{}'); } catch (e) {}
              if (res.status >= 200 && res.status < 300) resolve({ ok: true, status: res.status, data: data });
              else reject(Object.assign(new Error('HTTP ' + res.status), { status: res.status, data: data }));
            },
            onerror() { reject(new Error('network error')); },
            ontimeout() { reject(new Error('timeout')); }
          };
          if (body !== null) req.data = body;
          GM_xmlhttpRequest(req);
          return;
        } catch (e) { /* fall through to fetch */ }
      }
      const fetchImpl = originalPageFetch || (typeof fetch === 'function' ? fetch : null);
      if (!fetchImpl) { reject(new Error('no fetch')); return; }
      const init = { method: method, headers: headers, credentials: 'omit' };
      if (body !== null) init.body = body;
      fetchImpl(url, init)
        .then((res) => res.json().catch(() => null).then((data) => {
          if (res.ok) resolve({ ok: true, status: res.status, data: data });
          else reject(Object.assign(new Error('HTTP ' + res.status), { status: res.status, data: data }));
        }))
        .catch(reject);
    });
  }

  // 离线队列：网关没开时先落 GM 存储，恢复后按顺序补发
  const OUTBOX_KEY = 'ocs_progress_outbox';
  const OUTBOX_MAX = 100;
  let flushingOutbox = false;
  function readOutbox() {
    try {
      const raw = gmGet(OUTBOX_KEY, '');
      const list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (e) { return []; }
  }
  function writeOutbox(list) {
    gmSet(OUTBOX_KEY, JSON.stringify(list.slice(-OUTBOX_MAX)));
  }
  function queuePayload(path, payload) {
    const item = { id: path + '|' + JSON.stringify(payload || {}), path: path, payload: payload, at: Date.now(), tries: 0 };
    const list = readOutbox();
    if (list.some((x) => x.id === item.id)) return;
    list.push(item);
    writeOutbox(list);
  }
  function flushOutbox() {
    if (flushingOutbox) return;
    if (NAV && NAV.onLine === false) return;
    const list = readOutbox();
    if (!list.length) return;
    flushingOutbox = true;
    const item = list[0];
    postJSON(item.path, item.payload, { fromQueue: true })
      .then(() => {
        writeOutbox(readOutbox().filter((x) => x.id !== item.id));
        flushingOutbox = false;
        if (readOutbox().length) setTimeout(flushOutbox, 800);
      })
      .catch((err) => {
        item.tries = (item.tries || 0) + 1;
        const now = readOutbox();
        const idx = now.findIndex((x) => x.id === item.id);
        if (idx >= 0) {
          now[idx].tries = item.tries;
          if (item.tries > 8 || Date.now() - item.at > 86400000) now.splice(idx, 1);
          writeOutbox(now);
        }
        lastError = String((err && err.message) || err || '');
        flushingOutbox = false;
      });
  }

  // ======== 页面角色与多标签治理 ========
  // @match 会在每个 frame 里各跑一份脚本；主 frame 与子 frame 同时上报会重复，
  // 多个课程标签页同时打开也会重复。这里用 Web Locks 选出一个「主标签」，
  // 再用 BroadcastChannel 通知同页其它 frame 已有��在上报。
  const FRAME_ROLE = (() => {
    try { return window.top === window ? 'top' : 'child'; } catch (e) { return 'child'; }
  })();
  let topReachable = false;
  try { topReachable = Boolean(window.top && window.top.document); } catch (e) { topReachable = false; }

  const CHANNEL_NAME = 'ocs-progress-sync';
  let channel = null;
  // Node 里没有 window；BroadcastChannel 会让 node --test 一直挂着不退出。
  if (typeof window !== 'undefined' && typeof BroadcastChannel === 'function') {
    try { channel = new BroadcastChannel(CHANNEL_NAME); } catch (e) {}
  }
  let peerReportedAt = 0;
  if (channel) {
    try {
      channel.onmessage = (event) => {
        const data = event && event.data;
        if (!data || typeof data !== 'object') return;
        if (data.type === 'reported') peerReportedAt = Date.now();
      };
    } catch (e) {}
  }

  let isLeader = FRAME_ROLE === 'top';
  let leaderRelease = null;
  function broadcastReported() {
    if (!channel) return;
    try { channel.postMessage({ type: 'reported', at: Date.now() }); } catch (e) {}
  }

  function releaseLeadership() {
    isLeader = false;
    if (leaderRelease) {
      const release = leaderRelease;
      leaderRelease = null;
      try { release(); } catch (e) {}
    }
  }

  function electLeader() {
    // 只有主 frame 参与选主；子 frame 跟随主 frame
    if (FRAME_ROLE !== 'top') return Promise.resolve();
    const locks = NAV && NAV.locks;
    if (!locks || typeof locks.request !== 'function') {
      isLeader = true;
      return Promise.resolve();
    }
    // 抢到锁的就是主标签；页面隐藏时释放锁，另一个可见标签自动接管，
    // 避免「主标签在后台 → 谁都不上报」。
    return Promise.resolve()
      .then(() => locks.request('ocs-progress-leader', () => {
        isLeader = true;
        return new Promise((resolve) => { leaderRelease = resolve; });
      }))
      .catch(() => { isLeader = true; });
  }

  function canReport() {
    if (!isLeader) return false;
    if (FRAME_ROLE === 'top') return true;
    // 跨域 iframe（拿不到 top.document）里可能才是真正的课程内容，只能自己报
    if (!topReachable) return true;
    // 同页主 frame 20 秒内报过，就不重复报
    return Date.now() - peerReportedAt > 20000;
  }

  function send(payload) {
    const data = Object.assign({}, payload, {
      client_id: CLIENT_ID,
      seq: nextSeq(),
      ver: probeVersion(),
      ts: new Date().toISOString()
    });
    delete data.token;
    data.frame_role = FRAME_ROLE;
    return postJSON('/api/progress', data)
      .then(() => { lastSuccessAt = Date.now(); lastError = ''; broadcastReported(); })
      .catch((err) => {
        lastError = String((err && err.message) || err || '');
        queuePayload('/api/progress', data);
        if (CONFIG.debug) console.warn('[OCS 进度] 上报失败，已入队', err);
      });
  }

  function detectCourse() {
    const el = document.querySelector('.j-course-card-box .title, .course-card-wrapper .title, .courseTxt, .m-coursetitle, .courseName, .course-name, .course-title, .j-courseName, .course-info .title, h1, .u-courseName');
    let text = el && (el.innerText || el.textContent || '').trim();
    const junk = /^(网易|icourse163|中国大学MOOC|学习通|超星|学堂在线)/i;
    if (text && junk.test(text)) text = '';
    if (!text) text = (document.title || '').split(/[—|]/)[0].split(/\s-\s/)[0].replace(/[-_].*$/, '').trim();
    if (text && junk.test(text)) text = '';
    return (text || '').slice(0, 120);
  }

  function squeeze(s) {
    return String(s || '').replace(/\s+/g, ' ').replace(/^(已学完|已完成|已学|未完成|待完成|未开始)\s*/, '').trim();
  }

  function isLearnPage() {
    return /\/learn\/|courseware|knowledge|\/work\/|mycourse|lesson|chapter|\/mooc-ans\//i.test(location.href);
  }

  function pickCurrentNode() {
    const nodes = document.querySelectorAll('[class*="current"], [class*="active"], [class*="selected"]');
    let best = null;
    let bestScore = 0;
    nodes.forEach((node) => {
      const t = squeeze(node.innerText || node.textContent);
      if (!t || t.length > 90) return;
      const cls = String(node.className || '').toLowerCase();
      let score = 0;
      if (/chapter|section|unit|lesson|catalog|knowledge|task|point/.test(cls)) score += 3;
      if (!node.querySelector('div, li, ul, a')) score += 1;
      if (score > bestScore) { bestScore = score; best = node; }
    });
    return bestScore >= 3 ? best : null;
  }

  function detectPosition() {
    let chapter = '';
    let section = '';
    const learn = isLearnPage();
    const chapEl = document.querySelector('.u-learn-moduletitle .j-chapter, .titleBar .j-chapter, .j-chapter, .chapter-title, .chapter-head');
    const secEl = document.querySelector('.u-learn-moduletitle .j-lesson, .titleBar .j-lesson, .j-lesson, .section-title');
    if (chapEl) chapter = squeeze(chapEl.innerText || chapEl.textContent);
    if (secEl) section = squeeze(secEl.innerText || secEl.textContent);
    const unitEl = document.querySelector('.j-unit.current .unit-name, .j-unit.active .unit-name, .unit-name.current, .unit-name.active');
    if (unitEl) {
      const u = squeeze(unitEl.innerText || unitEl.textContent);
      if (u) section = section ? section + ' · ' + u : u;
    }
    const node = (chapter && section) ? null : pickCurrentNode();
    if (node) {
      section = squeeze(node.innerText || node.textContent);
      let p = node.parentElement;
      for (let i = 0; p && i < 6; i += 1, p = p.parentElement) {
        const cls = String(p.className || '').toLowerCase();
        if (!/chapter|unit|knowledge|catalog/.test(cls)) continue;
        const title = p.querySelector('.chapter-title, .chapter-head, .title, h2, h3, h4');
        const t = squeeze(title ? (title.innerText || title.textContent) : '');
        if (t && t !== section && t.length <= 90) { chapter = t; break; }
      }
      if (!chapter) {
        const box = node.closest('li, .item, .unit, .lesson');
        const prev = box && box.previousElementSibling;
        const t = squeeze(prev ? (prev.innerText || prev.textContent) : '');
        if (t && t.length <= 90) chapter = t;
      }
    }
    if (!chapter && learn) {
      const crumb = document.querySelector('.breadcrumb, .crumbs, .position, .prev_ul, .u-crumb, #position, .m-crumb');
      if (crumb) {
        const parts = squeeze(crumb.innerText || crumb.textContent).split(/[\/>»]/).map((s) => s.trim()).filter(Boolean);
        chapter = parts.length > 1 ? parts[parts.length - 1] : (parts[0] || '');
      }
    }
    if (!chapter && learn) {
      const parts = String(document.title || '').split(/[-_|]/).map((s) => s.trim())
        .filter((s) => s && !/中国大学MOOC|慕课|网易|学习通|超星|学堂在线/i.test(s));
      if (parts.length >= 3) chapter = parts[parts.length - 2] || '';
      else if (parts.length === 2) chapter = parts[1];
    }
    return { chapter: chapter.slice(0, 120), section: section.slice(0, 120), live: learn };
  }

  /* 学习通章节/任务点：选择器取自 OCS 源码（getTeacherAjax + jobUnfinishCount + attachments） */
  function cxRootDoc() {
    try {
      if (window.top && window.top.document && /mycourse\/studentstudy/.test(location.href)) return window.top.document;
    } catch (e) { /* cross-origin */ }
    return document;
  }

  function detectCxChapters() {
    try {
      const doc = cxRootDoc();
      const nodes = Array.prototype.slice.call(doc.querySelectorAll('[onclick^="getTeacherAjax"]'));
      if (!nodes.length) return null;
      const rows = nodes.map(function (el) {
        const m = String(el.getAttribute('onclick') || '').match(/\('(.*?)','(.*?)','(.*?)'\)/);
        const id = m ? m[3] : '';
        const name = String(el.textContent || '').trim().slice(0, 80);
        const holder = el.parentElement;
        const counter = holder ? holder.querySelector('.jobUnfinishCount') : null;
        // 没有未完成计数器的章节不当作已完成（避免误报 100%）
        const unfinish = counter ? parseInt(counter.value || '0', 10) || 0 : null;
        return { id: id, name: name, unfinish: unfinish, done: unfinish === 0 };
      });
      const done = rows.filter(function (r) { return r.done; }).length;
      return { rows: rows, done: done, total: rows.length };
    } catch (e) { return null; }
  }

  function detectCxAttachments() {
    try {
      const win = window.top || window;
      const list = win.attachments;
      if (!Array.isArray(list) || !list.length) return null;
      const rows = list.map(function (a) {
        const p = a && a.property ? a.property : {};
        const name = p.name || p.title || (p.bookname ? p.bookname + (p.author || '') : '');
        const done = Boolean(a && (a.isPassed || !a.job));
        return { t: 'job', n: String(name || '未命名任务').slice(0, 80), c: '', l: '', d: done ? 1 : 0 };
      }).filter(function (r) { return r.n; });
      if (!rows.length) return null;
      const done = rows.filter(function (r) { return r.d === 1; }).length;
      return { rows: rows, done: done, total: rows.length };
    } catch (e) { return null; }
  }

  /* 待做作业/测验：只读扫描课程页里的作业/测验链接与状态，不进入、不提交 */
  function detectCxPending() {
    try {
      const out = [];
      const seen = {};
      const links = document.querySelectorAll('a[href]');
      for (let i = 0; i < links.length && out.length < 60; i++) {
        const a = links[i];
        const href = String(a.getAttribute('href') || '');
        const text = String(a.textContent || '').trim();
        if (!text || text.length > 80) continue;
        if (!/(作业|测验|考试|章节测试)/.test(text)) continue;
        if (!/(work|exam|test)/i.test(href)) continue;
        const key = href + '|' + text;
        if (seen[key]) continue;
        seen[key] = 1;
        let ctx = text;
        try {
          const box = a.closest('li,tr,div');
          if (box) ctx = String(box.innerText || text).replace(/\s+/g, ' ').trim().slice(0, 120);
        } catch (e) { /* ignore */ }
        const pending = /(未交|待提交|未提交|未做|待完成|未开始|待批阅)/.test(ctx)
                        && !/(已交|已提交|已完成|已批阅)/.test(ctx);
        out.push({
          t: /(考试|测验|章节测试)/.test(text) ? 'quiz' : 'homework',
          n: text.slice(0, 80),
          d: pending ? 0 : 1,
          status: ctx,
          url: /^https?:/.test(href) ? href : location.origin + (href.charAt(0) === '/' ? href : '/' + href)
        });
      }
      return out.length ? out : null;
    } catch (e) { return null; }
  }

  function progressRoot() {
    if (!DOC) return null;
    return DOC.querySelector('#courseLearn-inner-box, .m-learnbox, .m-learnChapterList, .course-card-wrapper, .j-course-card-box, .course-info, .courseInfo') || DOC.body;
  }

  function detectProgress() {
    const root = progressRoot();
    const body = root && root.innerText ? root.innerText : '';
    const ratioPatterns = [
      /已学\s*(\d+)\s*[\/／]\s*(\d+)\s*小节/,
      /任务点[^\d]{0,10}(\d+)\s*[\/／]\s*(\d+)/,
      /已完成[^\d]{0,10}(\d+)\s*[\/／]\s*(\d+)/,
      /(\d+)\s*[\/／]\s*(\d+)\s*个?(?:任务点|课|单元|章节)/
    ];
    for (const re of ratioPatterns) {
      const m = body.match(re);
      if (!m) continue;
      const done = Number(m[1]);
      const total = Number(m[2]);
      if (total > 0 && done >= 0 && done <= total) {
        return { done: done, total: total, scope: 'course', confidence: 0.62, source: 'dom-ratio' };
      }
    }

    const percentPatterns = [
      /学习进度[^\d]{0,12}(\d{1,3})\s*%/,
      /课程进度[^\d]{0,12}(\d{1,3})\s*%/,
      /已完成\s*(\d{1,3})\s*%/
    ];
    for (const re of percentPatterns) {
      const m = body.match(re);
      if (!m) continue;
      const percent = Number(m[1]);
      if (percent >= 0 && percent <= 100) {
        return { percent: percent, scope: 'course', confidence: 0.55, source: 'dom-percent' };
      }
    }

    if (!root) return null;
    const all = Array.from(root.querySelectorAll('[class*="task"], [class*="point"], [class*="chapter"], [class*="unit"]')).slice(0, 500);
    const marks = all.filter((node) => !node.querySelector('[class*="task"], [class*="point"], [class*="chapter"], [class*="unit"]'));
    let done = 0;
    let total = 0;
    marks.forEach((node) => {
      const cls = String(node.className || '');
      const txt = (node.innerText || '').trim();
      if (!txt) return;
      if (/已完成|已学完|已学/.test(txt) && !/未完成|未学/.test(txt)) {
        done += 1;
        total += 1;
      } else if (/未完成|待完成|未学|未开始/.test(txt)) {
        total += 1;
      } else if (/已完成|completed/i.test(cls)) {
        done += 1;
        total += 1;
      }
    });
    return total > 0 && done <= total ? { done: done, total: total, scope: 'course', confidence: 0.45, source: 'dom-marks' } : null;
  }

  function scanJsonForProgress(obj) {
    if (!obj || typeof obj !== 'object') return null;
    const doneRe = /^(finished|finish|completed|complete|done|learned|learnCount|finishedCount|finishCount)$/i;
    const totalRe = /^(total|totalCount|count|all|allCount|num|taskCount|chapterCount|unitCount|lessonCount)$/i;
    const percentRe = /^(progress|percent|percentage|rate|schedule|finishRate|learnRate|progressRate)$/i;
    let best = null;
    const num = (value) => {
      if (typeof value === 'number' && Number.isFinite(value)) return value;
      if (typeof value === 'string' && /^\d+(\.\d+)?$/.test(value.trim())) return Number(value.trim());
      return null;
    };
    const consider = (candidate) => {
      if (!candidate) return;
      if (!best || candidate.confidence > best.confidence) best = candidate;
    };
    const visit = (node, path, depth) => {
      if (!node || typeof node !== 'object' || depth > 8) return;
      if (Array.isArray(node)) {
        node.forEach((value, index) => visit(value, path + '[' + index + ']', depth + 1));
        return;
      }
      const keys = Object.keys(node);
      keys.forEach((key) => {
        const value = num(node[key]);
        if (value === null) return;
        if (percentRe.test(key) && value >= 0 && value <= 100) {
          const strong = /^(progress|percent|percentage|finishRate|learnRate|progressRate)$/i.test(key);
          consider({ percent: Math.round(value), path: path + '.' + key, confidence: strong ? 0.7 : 0.5, scope: 'api' });
        }
        if (!doneRe.test(key)) return;
        keys.forEach((totalKey) => {
          const total = num(node[totalKey]);
          if (total === null || !totalRe.test(totalKey)) return;
          if (value < 0 || total <= 0 || value > total) return;
          const strong = /^(total|totalCount|taskCount|chapterCount|unitCount|lessonCount|allCount)$/i.test(totalKey);
          consider({
            done: value,
            total: total,
            path: path + '.' + key + '/' + totalKey,
            confidence: strong ? 0.92 : 0.7,
            scope: 'api'
          });
        });
      });
      keys.forEach((key) => {
        const value = node[key];
        if (value && typeof value === 'object') visit(value, path + '.' + key, depth + 1);
      });
    };
    visit(obj, '', 0);
    return best;
  }

  let lastApiProgress = null;
  let lastApiSeenAt = 0;
  let lastReplayAt = 0;
  const RECIPE_PREFIX = 'ocs_progress_recipe::';
  const RECIPE_TTL = 7 * 86400000;

  function apiCourseKey() {
    try {
      return String(detectCourse() || '') + '|' + String(location.href.split('#')[0]);
    } catch (e) { return ''; }
  }

  function sanitizeRecipeUrl(raw) {
    try {
      const url = new URL(String(raw || ''), location.href);
      if (!/^https?:$/.test(url.protocol)) return '';
      const host = url.hostname.toLowerCase();
      const hostOk = url.origin === location.origin
        || /(^|\.)chaoxing\.com$/.test(host)
        || /(^|\.)icourse163\.org$/.test(host);
      if (!hostOk) return '';
      if (/\/(submit|save|finish|complete|update|delete|remove|reset|start|buy|pay|upload|commit|answer)(\/|$)/i.test(url.pathname)) return '';
      ['token', 'sign', 'signature', 'sid', 'session', 'auth', 'key', 'nonce', 'csrf', 'ut', 't', 'ticket']
        .forEach((key) => url.searchParams.delete(key));
      url.hash = '';
      const clean = url.href;
      return clean.length <= 400 ? clean : '';
    } catch (e) {
      return '';
    }
  }

  function saveRecipe(method, url) {
    try {
      if (String(method || 'GET').toUpperCase() !== 'GET') return;
      const clean = sanitizeRecipeUrl(url);
      if (!clean) return;
      gmSet(RECIPE_PREFIX + apiCourseKey(), JSON.stringify({ method: 'GET', url: clean, at: Date.now() }));
    } catch (e) {}
  }

  function loadRecipe() {
    try {
      const key = RECIPE_PREFIX + apiCourseKey();
      const recipe = JSON.parse(gmGet(key, '') || 'null');
      if (!recipe || recipe.method !== 'GET' || !recipe.url) return null;
      if (!recipe.at || Date.now() - recipe.at > RECIPE_TTL) { gmDel(key); return null; }
      if (sanitizeRecipeUrl(recipe.url) !== recipe.url) { gmDel(key); return null; }
      return recipe;
    } catch (e) { return null; }
  }

  async function replayRecipe() {
    const recipe = loadRecipe();
    // 只主动重放 GET：POST 可能是写接口（提交/保存），只能被动嗅探
    if (!recipe || Date.now() - lastReplayAt < 60000) return null;
    lastReplayAt = Date.now();
    try {
      const fetchImpl = originalPageFetch || (typeof fetch === 'function' ? fetch : null);
      if (!fetchImpl) return null;
      const resp = await fetchImpl(recipe.url, { credentials: 'include' });
      if (!resp.ok) return null;
      const ct = resp.headers.get('content-type') || '';
      if (!ct.includes('json')) return null;
      const p = scanJsonForProgress(await resp.json());
      if (p) {
        p.api = recipe.url.slice(0, 200);
        p.active = true;
        p.recipe = true;
        lastApiProgress = p;
        lastApiSeenAt = Date.now();
        return p;
      }
    } catch (e) {}
    return null;
  }

  function hookNetwork() {
    const target = PAGE;
    if (!target) return;
    try {
      if (target.__ocsProgressHooked) return;
      target.__ocsProgressHooked = true;
    } catch (e) {}

    const origFetch = target.fetch;
    if (typeof origFetch === 'function') {
      originalPageFetch = origFetch.bind(target);
      try {
        target.fetch = async function (...args) {
          const resp = await origFetch.apply(this, args);
          try {
            const clone = resp.clone();
            const ct = clone.headers.get('content-type') || '';
            if (ct.includes('json')) {
              const json = await clone.json();
              const p = scanJsonForProgress(json);
              if (p) {
                const init = args[1] || {};
                const reqUrl = String((args[0] && args[0].url) || args[0] || '');
                p.api = reqUrl.slice(0, 200);
                lastApiProgress = p;
                lastApiSeenAt = Date.now();
                saveRecipe(init.method || (args[0] && args[0].method) || 'GET', reqUrl);
              }
              try { const found = []; scanJsonForDeadlines(json, found, 0); if (found.length) lastApiDeadlines = found; } catch (e) {}
            }
          } catch (e) {}
          return resp;
        };
      } catch (e) {}
    }

    const XHR = target.XMLHttpRequest;
    if (XHR && XHR.prototype) {
      const proto = XHR.prototype;
      const origOpen = proto.open;
      const origSend = proto.send;
      proto.open = function (method, url, ...rest) {
        this.__ocsUrl = url;
        this.__ocsMethod = method;
        return origOpen.call(this, method, url, ...rest);
      };
      proto.send = function (...args) {
        this.addEventListener('load', () => {
          try {
            const ct = this.getResponseHeader('content-type') || '';
            if (!ct.includes('json')) return;
            const json = JSON.parse(this.responseText);
            const p = scanJsonForProgress(json);
            try { const found = []; scanJsonForDeadlines(json, found, 0); if (found.length) lastApiDeadlines = found; } catch (e) {}
            if (p) {
              p.api = String(this.__ocsUrl || '').slice(0, 200);
              lastApiProgress = p;
              lastApiSeenAt = Date.now();
              saveRecipe(this.__ocsMethod, this.__ocsUrl);
            }
          } catch (e) {}
        });
        return origSend.apply(this, args);
      };
    }
  }

  function detectCourses() {
    const raw = [];
    document.querySelectorAll('.j-course-card-box, .course-card-wrapper').forEach((card) => {
      const titleEl = card.querySelector('.title');
      const title = titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : '';
      const progEl = card.querySelector('.course-progress-text-span') || card.querySelector('.course-progress');
      const prog = progEl ? (progEl.innerText || progEl.textContent || '').trim() : '';
      const m = prog.match(/已学\s*(\d+)\s*[\/／]\s*(\d+)\s*小节/);
      const upd = m ? null : prog.match(/已更新\s*(\d+)\s*小节/);
      if (!title || (!m && !upd)) return;
      const done = m ? Number(m[1]) : 0;
      const total = m ? Number(m[2]) : Number(upd[1]);
      const link = card.closest('a') || card.querySelector('a') || card;
      const href = (link && link.getAttribute && link.getAttribute('href')) || '';
      const statusEl = card.querySelector('.course-status');
      const statusText = statusEl ? (statusEl.innerText || statusEl.textContent || '') : '';
      const dm = statusText.match(/(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日/);
      const deadline = dm ? `${dm[1]}-${String(dm[2]).padStart(2, '0')}-${String(dm[3]).padStart(2, '0')}` : '';
      raw.push({ course: title.slice(0, 120), done, total, href, deadline, updated: Boolean(upd) });
    });
    // 卡片链接若重复（例如都指向同一个列表页），改用「课程名|总小节」做区分
    const counts = {};
    raw.forEach((e) => { if (e.href) counts[e.href] = (counts[e.href] || 0) + 1; });
    const seen = new Set();
    const out = [];
    raw.forEach((e) => {
      const key = e.href && counts[e.href] === 1 ? e.href : e.course + '|' + e.total;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({
        course: e.course,
        done: e.done,
        total: e.total,
        key: key,
        deadline: e.deadline || '',
        updated: Boolean(e.updated),
        updated_count: e.updated ? e.total : undefined
      });
    });
    return out;
  }

  function unitType(el) {
    const tag = el.querySelector && el.querySelector('.tag');
    const label = tag ? (tag.textContent || '').trim() : '';
    const probe = label + ' ' + ((el.getAttribute && el.getAttribute('title')) || '');
    if (/视频|video/i.test(probe)) return 'video';
    if (/文档|doc|pdf|ppt/i.test(probe)) return 'doc';
    if (/测验|作业|考试|quiz|test|homework/i.test(probe)) return 'quiz';
    if (/讨论|discuss|bbs/i.test(probe)) return 'discuss';
    const iconCls = Array.from(el.querySelectorAll('[class*="icon"], [class*="video"], [class*="doc"], [class*="pdf"], [class*="ppt"], [class*="quiz"], [class*="discuss"], [class*="homework"], [class*="exam"]'))
      .map((n) => String(n.className || '')).join(' ');
    const blob = String(el.className || '') + ' ' + iconCls;
    if (/video|shipin/i.test(blob)) return 'video';
    if (/doc|pdf|ppt|richtext/i.test(blob)) return 'doc';
    if (/quiz|homework|exam|test|ceshi/i.test(blob)) return 'quiz';
    if (/discuss|bbs|comment|pinglun/i.test(blob)) return 'discuss';
    return '';
  }

  function unitDone(el) {
    const cls = String(el.className || '') + ' ' + Array.from(el.querySelectorAll('*')).slice(0, 8).map((n) => String(n.className || '')).join(' ');
    const txt = el.innerText || el.textContent || '';
    return /完成|已学|已通过|finish|complete|done|learned|passed/i.test(cls) || /已完成|已学完|已通过/.test(txt);
  }

  function countTasks(rows) {
    const counts = {};
    rows.forEach((el) => {
      const type = unitType(el);
      if (!type) return;
      if (!counts[type]) counts[type] = { done: 0, total: 0 };
      counts[type].total += 1;
      if (unitDone(el)) counts[type].done += 1;
    });
    return Object.keys(counts).length ? counts : null;
  }

  function detectTasks() {
    // 只有课件目录页能给出整门课的任务项；播放页只有当前一节，不能当整门课统计
    const chapters = document.querySelectorAll('.m-learnChapterList .m-learnChapterNormal');
    const rows = document.querySelectorAll('.m-learnChapterList .sourceList .lsicon');
    if (!chapters.length || !rows.length) return null;
    const loaded = Array.from(chapters).filter((c) => c.querySelector('.lessonBox .lsicon')).length;
    if (loaded < chapters.length) return null;
    return countTasks(rows);
  }

  let catalogExpandedFor = '';
  function expandCatalogOnce() {
    // 默认不主动点击页面；只有用户在设置里显式打开 autoExpand 才展开目录。
    if (!CONFIG.autoExpand) return;
    const key = location.pathname + location.search;
    if (catalogExpandedFor === key) return;
    const chapters = Array.from(document.querySelectorAll('.m-learnChapterList .m-learnChapterNormal'));
    if (!chapters.length) return;
    catalogExpandedFor = key;
    const collapsed = chapters.filter((c) => !c.querySelector('.lessonBox .lsicon'));
    if (!collapsed.length) return;
    // 目录把未展开章节的条目延迟渲染；只点开（只读 GET），拿到全量后再统计
    collapsed.forEach((chapter, i) => {
      const toggle = chapter.querySelector('.courseware-right .u-icon-caret-down');
      if (toggle && toggle.offsetParent) setTimeout(() => toggle.click(), 300 * i);
    });
    setTimeout(() => { if (document.visibilityState === 'visible') report(true); }, 300 * collapsed.length + 2500);
  }

  function detectTaskItems() {
    const chapters = document.querySelectorAll('.m-learnChapterList .m-learnChapterNormal');
    const rows = document.querySelectorAll('.m-learnChapterList .sourceList .lsicon');
    if (!rows.length) return null;
    if (chapters.length) {
      const loaded = Array.from(chapters).filter((c) => c.querySelector('.lessonBox .lsicon')).length;
      if (loaded < chapters.length) return null;
    }
    const items = [];
    rows.forEach((el) => {
      const type = unitType(el);
      if (!type) return;
      const tag = el.querySelector('.tag');
      const rawTitle = (el.getAttribute('title') || '').trim();
      let title = rawTitle.replace(/^(视频|文档|测验题|讨论题|讨论|测验)[:：]?\s*/, '');
      if (!title) title = String(el.textContent || '').replace(tag ? tag.textContent : '', '').trim();
      const lessonEl = el.closest('.u-learnLesson');
      const chapterEl = el.closest('.m-learnChapterNormal');
      const lesson = lessonEl && lessonEl.querySelector('.j-name') ? lessonEl.querySelector('.j-name').textContent.trim() : '';
      const chapter = chapterEl && chapterEl.querySelector('.j-titleName') ? chapterEl.querySelector('.j-titleName').textContent.trim() : '';
      items.push({ t: type, n: title.slice(0, 80), c: chapter.slice(0, 40), l: lesson.slice(0, 40), k: (el.getAttribute && el.getAttribute('data-cid')) || '', d: unitDone(el) ? 1 : 0 });
      if (items.length >= 400) return;
    });
    return items.length ? items : null;
  }

  // 截止日期的上下文关键词：出现「截止/结束」的是截止日期，出现「发布/开始」的是发布日期。
  const DEADLINE_WORD = /^(截止|结束|到期|过期|剩余|deadline|expire|close|stop|end)$/i;
  const PUBLISH_WORD = /^(发布|开始|创建|开课|开放|publish|begin|create|start)$/i;
  const KEYWORD_RE = /(截止|结束|到期|过期|剩余|deadline|expire|close|stop|end|发布|开始|创建|开课|开放|publish|begin|create|start)/gi;

  function parseDeadline(text) {
    const raw = String(text || '').replace(/\s+/g, ' ').trim();
    if (!raw) return '';
    const two = (n) => String(n).padStart(2, '0');
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const cands = [];

    const push = (year, month, day, hour, minute, index, length, hasYear) => {
      let y = Number(year);
      let mo = Number(month);
      let d = Number(day);
      const h = hour ? Number(hour) : 0;
      const mi = minute ? Number(minute) : 0;
      if (!hasYear) y = today.getFullYear();
      if (y < 2000 || y > 2100 || mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59) return;
      const dt = new Date(y, mo - 1, d, h, mi);
      if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return;
      // 没有年份时，若已过去 30 天以上，视为跨年后的日期
      if (!hasYear && dt.getTime() < today.getTime() - 30 * 86400000) dt.setFullYear(dt.getFullYear() + 1);
      cands.push({ dt: dt, index: index, length: length, hasTime: Boolean(h || mi), hasYear: hasYear, score: 0 });
    };

    let rel = raw.match(/(今天|明天|后天)(?:\s*(\d{1,2}):(\d{2}))?/);
    if (rel) {
      const base = new Date(today);
      base.setDate(base.getDate() + ({ 今天: 0, 明天: 1, 后天: 2 }[rel[1]] || 0));
      push(base.getFullYear(), base.getMonth() + 1, base.getDate(), rel[2], rel[3], rel.index, rel[0].length, true);
    }
    rel = raw.match(/剩余\s*(\d+)\s*天/);
    if (rel) {
      const base = new Date(today);
      base.setDate(base.getDate() + Number(rel[1]));
      push(base.getFullYear(), base.getMonth() + 1, base.getDate(), '', '', rel.index, rel[0].length, true);
    }

    const reFull = /(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?(?:\s*(\d{1,2}):(\d{2}))?/g;
    let m;
    while ((m = reFull.exec(raw))) {
      push(m[1], m[2], m[3], m[4], m[5], m.index, m[0].length, true);
    }
    const reNoYear = /(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s*(\d{1,2}):(\d{2}))?/g;
    while ((m = reNoYear.exec(raw))) {
      const before = raw[m.index - 1];
      // 「2026年3月5日」里的「3月5日」是带年份那一支的后缀，不要重复当成无年份日期
      if (before && (/\d/.test(before) || before === '年')) continue;
      push(today.getFullYear(), m[1], m[2], m[3], m[4], m.index, m[0].length, false);
    }
    if (!cands.length) return '';
    cands.sort((a, b) => a.index - b.index);

    // 关键词与日期的绑定：「截止日期 2026-11-08」里的「截止」属于后面那个日期，
    // 「发布日期 2026-12-31」里的「发布」属于后面那个日期。靠这个才能在
    // 一句话里有两个日期时选对，而不是简单取更晚的那个。
    const keywords = [];
    KEYWORD_RE.lastIndex = 0;
    let km;
    while ((km = KEYWORD_RE.exec(raw))) {
      keywords.push({ start: km.index, end: km.index + km[0].length, word: km[0] });
    }
    keywords.forEach((word) => {
      const after = cands.find((c) => c.index >= word.end);
      if (after) {
        const dist = after.index - word.end;
        const weight = DEADLINE_WORD.test(word.word) ? 3 : (PUBLISH_WORD.test(word.word) ? -2 : 0);
        after.score += weight + (weight > 0 ? Math.max(0, 1 - dist / 12) : 0);
        return;
      }
      // 关键词在最后一个日期之后（如「2026-11-08 截止」）
      let prev = null;
      for (const c of cands) {
        if (c.index + c.length <= word.start) prev = c;
      }
      if (!prev) return;
      const dist = word.start - (prev.index + prev.length);
      const weight = DEADLINE_WORD.test(word.word) ? 3 : (PUBLISH_WORD.test(word.word) ? -2 : 0);
      prev.score += weight * 0.9 + (weight > 0 ? Math.max(0, 1 - dist / 12) : 0);
    });

    cands.forEach((c) => {
      if (c.hasTime) c.score += 1;
      if (c.hasYear) c.score += 1;
      if (c.dt.getTime() >= today.getTime()) c.score += 0.5;
    });
    cands.sort((a, b) => (b.score - a.score) || (b.dt.getTime() - a.dt.getTime()));
    const best = cands[0];
    const d = best.dt;
    const date = `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
    return best.hasTime ? `${date} ${two(d.getHours())}:${two(d.getMinutes())}` : date;
  }

  function isTestListPage() {
    if (!/icourse163/.test(location.host)) return false;
    const href = location.href;
    if (/testlist|assignment|homework|exam|test\/list|quiz/i.test(href)) return true;
    if (/\/learn\//.test(href)) {
      const text = (document.body && document.body.innerText) || '';
      if (/测验与作业|测验和作业|考试列表|作业列表/.test(text)) return true;
    }
    return document.querySelectorAll('a[href*="aid="]').length >= 2;
  }

  function detectVideoStats() {
    const text = (document.body && document.body.innerText) || '';
    const dur = text.match(/视频学习时长[：:]\s*(?:(\d+)\s*时)?\s*(\d+)\s*分/);
    const cnt = text.match(/视频观看个数[：:]\s*(\d+)/);
    const les = text.match(/课时学习进度[：:]\s*(\d+)\s*[\/／]\s*(\d+)/);
    if (!dur && !cnt && !les) return null;
    return {
      minutes: dur ? (Number(dur[1] || 0) * 60 + Number(dur[2] || 0)) : null,
      count: cnt ? Number(cnt[1]) : null,
      lesson_done: les ? Number(les[1]) : null,
      lesson_total: les ? Number(les[2]) : null,
    };
  }

  function fmtStamp(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return '';
    const ms = n < 1e12 ? n * 1000 : n;
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return '';
    const two = (x) => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
  }

  let lastApiDeadlines = [];
  function scanJsonForDeadlines(obj, out, depth) {
    depth = depth || 0;
    if (!obj || typeof obj !== 'object' || out.length >= 100 || depth > 7) return;
    if (Array.isArray(obj)) {
      obj.forEach((item) => scanJsonForDeadlines(item, out, depth + 1));
      return;
    }
    const keys = Object.keys(obj);
    const nameKey = keys.find((k) => /^(name|title|testName|quizName|homeworkName|paperName)$/i.test(k));
    const dateKey = keys.find((k) => /(deadline|endTime|endDate|expire|closeTime|end_time|stopTime)/i.test(k));
    if (nameKey && dateKey) {
      const name = String(obj[nameKey] || '').trim();
      const raw = obj[dateKey];
      const deadline = typeof raw === 'number' ? fmtStamp(raw) : parseDeadline(String(raw || ''));
      if (name && deadline) {
        const aid = obj.id || obj.testId || obj.quizId || obj.homeworkId || obj.paperId || '';
        const tid = obj.tid || obj.termId || '';
        const statusRaw = String(obj.status || obj.submitStatus || obj.answerStatus || '');
        const status = /(NOT|UN|未)/i.test(statusRaw) ? 'unsubmitted'
          : (/DONE|FINISH|已/.test(statusRaw) ? 'done' : '');
        out.push({ tid: String(tid), aid: String(aid), name: name.slice(0, 120),
          kind: /考试/.test(name) ? '考试' : (/作业/.test(name) ? '作业' : ''), deadline, status });
      }
    }
    keys.forEach((k) => {
      const value = obj[k];
      if (value && typeof value === 'object') scanJsonForDeadlines(value, out, depth + 1);
    });
  }

  function detectDeadlineItems() {
    const out = [];
    const seen = new Set();
    document.querySelectorAll('a[href]').forEach((a) => {
      const href = a.getAttribute('href') || '';
      const tid = (href.match(/[?&#]tid=(\d+)/) || [])[1];
      const aid = (href.match(/[?&#]aid=(\d+)/) || [])[1];
      if (!tid || !aid) return;
      const box = a.closest('li, tr, .item, .card, .quiz-item, div') || a.parentElement;
      const text = ((box && box.innerText) || a.innerText || a.getAttribute('title') || '')
        .replace(/\s+/g, ' ').trim();
      if (!text) return;
      let name = (a.innerText || a.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
      if (!name) name = text.slice(0, 60);
      const key = tid + ':' + aid;
      if (seen.has(key)) return;
      seen.add(key);
      const deadline = parseDeadline(text) || parseDeadline([
        box && box.getAttribute('data-deadline'),
        box && box.getAttribute('data-end-time'),
        box && box.getAttribute('data-endtime'),
        a.getAttribute('title')
      ].filter(Boolean).join(' '));
      const kind = /考试/.test(text) ? '考试' : (/作业/.test(text) ? '作业' : (/测验|测试/.test(text) ? '测验' : ''));
      const status = /未提交|未完成|待完成|未参加|未开始/.test(text) ? 'unsubmitted'
        : (/待批改|待评分|待阅/.test(text) ? 'pending'
          : (/已完成|已提交|已批改|已通过/.test(text) ? 'done' : ''));
      out.push({ tid, aid, name: name.slice(0, 120), kind, deadline, status });
    });
    // 第二遍：没有带 tid/aid 的链接时，从行容器 + data-* 属性里找
    if (!out.length) {
      const rows = document.querySelectorAll('li, tr, [class*="test"], [class*="quiz"], [class*="exam"], [class*="homework"], [class*="item"]');
      let scanned = 0;
      for (const row of rows) {
        if (scanned >= 60 || out.length >= 60) break;
        const text = (row.innerText || '').replace(/\s+/g, ' ').trim();
        if (!text || text.length > 400) continue;
        const deadline = parseDeadline(text) || parseDeadline([
          row.getAttribute('data-deadline'),
          row.getAttribute('data-end-time'),
          row.getAttribute('data-endtime'),
          row.getAttribute('title')
        ].filter(Boolean).join(' '));
        if (!deadline) continue;
        const html = row.outerHTML.slice(0, 2000);
        const tid = (html.match(/tid["'\s:=]+(\d+)/) || [])[1] || '';
        const aid = (html.match(/aid["'\s:=]+(\d+)/) || [])[1]
          || (html.match(/data-(?:id|testid|quizid)["'\s:=]+(\d+)/) || [])[1] || '';
        const name = text.split(/\s{2,}|\n/)[0].slice(0, 120);
        if (!name) continue;
        const key = tid + ':' + aid + ':' + name;
        if (seen.has(key)) continue;
        seen.add(key);
        const kind = /考试/.test(text) ? '考试' : (/作业/.test(text) ? '作业' : (/测验|测试/.test(text) ? '测验' : ''));
        const status = /未提交|未完成|待完成|未参加|未开始/.test(text) ? 'unsubmitted'
          : (/待批改|待评分|待阅/.test(text) ? 'pending'
            : (/已完成|已提交|已批改|已通过/.test(text) ? 'done' : ''));
        out.push({ tid, aid, name, kind, deadline, status });
        scanned += 1;
      }
    }
    const bodyText = (document.body && document.body.innerText) || '';
    const countMatch = bodyText.match(/共\s*(\d+)\s*个?(?:测验|作业|考试)/);
    const expected = countMatch ? Number(countMatch[1]) : 0;
    const partial = (expected && out.length < expected) || (!out.length && /测验|作业|考试/.test(bodyText));
    let note = '';
    if (partial) {
      note = expected ? `页面标注 ${expected} 个，只读到 ${out.length} 个（可能未展开或懒加载）` : '页面有测验/作业区，但未读到条目';
    }
    return { items: out, partial, expected, note };
  }

  function sendDeadlines(payload) {
    const data = Object.assign({}, payload, {
      client_id: CLIENT_ID,
      seq: nextSeq(),
      ver: probeVersion(),
      ts: new Date().toISOString()
    });
    delete data.token;
    return postJSON('/api/deadlines', data)
      .then(() => { lastSuccessAt = Date.now(); lastError = ''; })
      .catch((err) => {
        lastError = String((err && err.message) || err || '');
        queuePayload('/api/deadlines', data);
        if (CONFIG.debug) console.warn('[OCS 进度] DDL 上报失败，已入队', err);
      });
  }

  let sceneSentFor = '';
  function detectScene() {
    const text = ((document.body && document.body.innerText) || '').slice(0, 8000);
    const title = document.title || '';
    if (/随堂/.test(text) || /随堂/.test(title)) return 'quick';
    if (/单元测验|单元测试|单元作业|测验与作业|期末考试|期中考试/.test(text)) return 'unit';
    if (/test|exam|quiz|homework|assignment/i.test(location.href)) return 'unit';
    return '';
  }

  function reportScene() {
    const scene = detectScene();
    if (!scene) return;
    const text = ((document.body && document.body.innerText) || '').slice(0, 8000);
    const totalMatch = text.match(/共\s*(\d+)\s*(?:道|个)?\s*题/);
    const total = totalMatch ? Number(totalMatch[1]) : 0;
    const tid = ((location.href.match(/[?&#]tid=(\d+)/) || [])[1]) || '';
    const key = scene + '|' + location.pathname + '|' + total;
    if (sceneSentFor === key) return;
    sceneSentFor = key;
    const data = {
      scene: scene,
      url: location.href,
      total: total,
      tid: tid,
      client_id: CLIENT_ID,
      seq: nextSeq(),
      ver: probeVersion(),
      ts: new Date().toISOString()
    };
    postJSON('/api/scene', data).catch((err) => {
      lastError = String((err && err.message) || err || '');
      if (CONFIG.debug) console.warn('[OCS 进度] 场景上报失败', err);
    });
  }

  let reportInFlight = false;
  async function report(force) {
    if (reportInFlight) return;
    if (!force && document.visibilityState === 'hidden') return;
    if (!canReport()) return;
    reportInFlight = true;
    try {
      await reportInner(force);
    } finally {
      reportInFlight = false;
    }
  }

  async function reportInner(force) {
    reportScene();
    if (!lastApiProgress || Date.now() - lastApiSeenAt > 60000) {
      await replayRecipe().catch(() => null);
    }
    expandCatalogOnce();
    const videoStats = detectVideoStats();
    const courses = detectCourses();
    if (courses.length) {
      const account = detectAccount();
      courses.forEach((c) => {
        const updated = Boolean(c.updated);
        send({
          account,
          course: c.course,
          done: updated ? 0 : c.done,
          total: updated ? 0 : c.total,
          key: c.key,
          deadline: c.deadline || '',
          percent: null,
          url: location.href,
          live: false,
          extra: Object.assign(
            { source: 'course-card', kind: updated ? 'updated' : 'progress', updated_count: updated ? c.updated_count : undefined },
            videoStats ? { video_stats: videoStats } : {}
          )
        });
      });
      return;
    }
    const course = detectCourse();
    if (!course) return;
    const pending = detectCxPending();
    const learn = isLearnPage();
    if (!learn) {
      if (pending) {
        send({
          account: detectAccount(),
          course: course,
          done: 0,
          total: 0,
          percent: null,
          live: false,
          url: location.href,
          token: CONFIG.token,
          extra: { source: 'cx-pending', pending: pending, ver: probeVersion() }
        });
      }
      return;
    }
    if (isTestListPage()) {
      const found = detectDeadlineItems();
      const merged = found.items.slice();
      const seen = new Set(merged.map((i) => i.tid + ':' + i.aid + ':' + i.name));
      lastApiDeadlines.forEach((item) => {
        const key = item.tid + ':' + item.aid + ':' + item.name;
        if (!seen.has(key)) { seen.add(key); merged.push(item); }
      });
      const anchors = document.querySelectorAll('a[href]');
      let withTid = 0; let withAid = 0; let withBoth = 0;
      anchors.forEach((a) => {
        const h = a.getAttribute('href') || '';
        const t = /[?&#]tid=(\d+)/.test(h);
        const d = /[?&#]aid=(\d+)/.test(h);
        if (t) withTid += 1;
        if (d) withAid += 1;
        if (t && d) withBoth += 1;
      });
      const diag = `dom=${found.items.length},api=${lastApiDeadlines.length},anchors=${anchors.length}(tid ${withTid}/aid ${withAid}/both ${withBoth})`;
      const note = [found.note, diag].filter(Boolean).join('；');
      sendDeadlines({ account: detectAccount(), course, items: merged,
        partial: found.partial || merged.length < found.expected,
        expected: found.expected, note, url: location.href, token: CONFIG.token,
        extra: { source: 'testlist' } });
      return;
    }
    const pos = detectPosition();
    const tasks = detectTasks();
    const items = detectTaskItems();
    const taskExtra = {};
    if (tasks) taskExtra.tasks = tasks;
    if (items) taskExtra.items = items;
    const cxChapters = detectCxChapters();
    const cxAttachments = detectCxAttachments();
    if (cxChapters) {
      send({
        account: detectAccount(),
        course,
        done: cxChapters.done,
        total: cxChapters.total,
        percent: null,
        chapter: pos.chapter,
        section: pos.section,
        live: true,
        url: location.href,
        token: CONFIG.token,
        extra: {
          source: 'cx-chapters',
          tasks: { chapter: { done: cxChapters.done, total: cxChapters.total },
                   job: cxAttachments ? { done: cxAttachments.done, total: cxAttachments.total } : undefined },
          items: cxAttachments ? cxAttachments.rows : cxChapters.rows.map(function (r) {
            return { t: 'chapter', n: r.name, c: r.name, l: '', d: r.done ? 1 : 0, unfinish: r.unfinish };
          }),
          pending: pending || undefined,
          ver: probeVersion()
        }
      });
      return;
    }
    const progress = lastApiProgress || detectProgress();
    if (!progress || (!progress.total && progress.percent == null)) {
      const hasPosition = Boolean(pos.chapter || pos.section);
      if (!hasPosition && !tasks && !items) return;
      send({
        account: detectAccount(),
        course,
        chapter: pos.chapter,
        section: pos.section,
        live: hasPosition,
        position_only: true,
        url: location.href,
        token: CONFIG.token,
        extra: Object.assign({ source: 'position' }, taskExtra)
      });
      return;
    }
    send({
      account: detectAccount(),
      course,
      done: progress.done || 0,
      total: progress.total || 0,
      percent: progress.percent == null ? null : progress.percent,
      chapter: pos.chapter,
      section: pos.section,
      live: pos.live,
      url: location.href,
      extra: Object.assign({
        source: lastApiProgress ? 'api' : 'dom',
        api: lastApiProgress ? lastApiProgress.api : undefined,
        active: lastApiProgress ? !!lastApiProgress.active : undefined,
        recipe: lastApiProgress ? !!lastApiProgress.recipe : undefined,
        scope: progress.scope || (lastApiProgress ? 'api' : 'course'),
        confidence: progress.confidence == null ? undefined : progress.confidence,
        ver: probeVersion()
      }, taskExtra)
    });
  }

  if (typeof window !== 'undefined') window.__ocsProgressReport = (done, total, percent, nonce) => {
    if (!CONFIG.debug && nonce !== CLIENT_ID) return false;
    const pos = detectPosition();
    send({
      account: detectAccount(),
      course: detectCourse(),
      done,
      total,
      percent: percent == null ? null : percent,
      chapter: pos.chapter,
      section: pos.section,
      live: true,
      url: location.href,
      extra: { source: 'manual', scope: 'manual' }
    });
    return true;
  };

  function heartbeat() {
    const data = {
      name: 'ocs-progress',
      ver: probeVersion(),
      url: location.href,
      client_id: CLIENT_ID,
      pending: readOutbox().length,
      last_success_at: lastSuccessAt || null,
      last_error: lastError || '',
      reporting: reportInFlight,
      frame_role: FRAME_ROLE,
      leader: isLeader,
      ts: new Date().toISOString()
    };
    postJSON('/api/probe/heartbeat', data).catch((err) => {
      lastError = String((err && err.message) || err || '');
    });
  }

  // 脚本版本体检：网关提供的版本比本地脚本新时提示升级
  function checkScriptVersion() {
    const localVer = probeVersion();
    if (!localVer) return;
    postJSON('/api/tools/versions', null, { method: 'GET', timeout: 6000 })
      .then((res) => {
        const served = (res && res.data && res.data.served) || {};
        const latest = served['ocs-progress.user.js'] || '';
        if (latest && latest !== localVer) {
          toast('OCS 进度脚本有新版本 ' + latest + '（当前 ' + localVer + '），请在脚本管理器里更新');
          if (CONFIG.debug) console.warn('[OCS 进度] 版本落后', localVer, latest);
        }
      })
      .catch(() => {});
  }

  let reportTimer = null;
  function scheduleReport() {
    clearTimeout(reportTimer);
    const hidden = DOC && DOC.visibilityState === 'hidden';
    const base = hidden ? Math.max(CONFIG.interval, 300000) : CONFIG.interval;
    const jitter = Math.floor(Math.random() * 3000);
    reportTimer = setTimeout(async () => {
      await report();
      scheduleReport();
    }, base + jitter);
  }

  function installRouteWatcher() {
    let lastHref = location.href;
    const check = () => {
      if (location.href === lastHref) return;
      lastHref = location.href;
      lastApiProgress = null;
      lastApiSeenAt = 0;
      lastApiDeadlines = [];
      setTimeout(() => report(true), 1200);
    };
    try {
      ['pushState', 'replaceState'].forEach((name) => {
        const orig = history[name];
        if (typeof orig !== 'function') return;
        history[name] = function (...args) {
          const result = orig.apply(this, args);
          setTimeout(check, 0);
          return result;
        };
      });
    } catch (e) {}
    try { addEventListener('popstate', check); } catch (e) {}
    try { addEventListener('hashchange', check); } catch (e) {}
    try { if (PAGE && 'onurlchange' in PAGE) PAGE.addEventListener('urlchange', check); } catch (e) {}
    setInterval(check, 3000);
  }

  // 供 Node 单元测试直接 require；浏览器里 module 不存在，继续正常启动。
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      scanJsonForProgress: scanJsonForProgress,
      parseDeadline: parseDeadline,
      sanitizeRecipeUrl: sanitizeRecipeUrl,
      detectProgress: detectProgress,
      detectCourses: detectCourses,
      pickCurrentNode: pickCurrentNode,
      detectPosition: detectPosition
    };
    return;
  }

  hookNetwork();
  heartbeat();
  flushOutbox();
  checkScriptVersion();
  setInterval(heartbeat, 300000);
  installRouteWatcher();
  electLeader();
  setTimeout(() => report(true), 3000);
  scheduleReport();
  try { addEventListener('online', flushOutbox); } catch (e) {}
  if (DOC) {
    DOC.addEventListener('visibilitychange', () => {
      if (DOC.visibilityState === 'visible') {
        flushOutbox();
        // 重新抢一次主标签：后台标签会让出主位给可见标签
        if (FRAME_ROLE === 'top') {
          releaseLeadership();
          electLeader();
        }
        setTimeout(() => report(true), 1000);
      } else if (FRAME_ROLE === 'top') {
        // 页面切到后台：让出主标签，避免「主标签在后台 → 谁都不上报」
        releaseLeadership();
      }
      scheduleReport();
    });
  }
})();
