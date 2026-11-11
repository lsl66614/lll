// ==UserScript==
// @name         OCS 进度探针（只读诊断）
// @namespace    ocs-ai-gateway
// @version      0.4.0
// @description  只读扫描学习通 / 中国大学MOOC 课程页，列出账号、课程、进度候选与命中的选择器，用于校准正式进度脚本。
// @match        *://*.chaoxing.com/*
// @match        *://*.icourse163.org/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    http://127.0.0.1:8000/tools/chaoxing-probe.meta.js
// @downloadURL  http://127.0.0.1:8000/tools/chaoxing-probe.user.js
// @run-at       document-idle
// @homepageURL  https://github.com/liu620305-jpg/ocs-ai-gateway
// @supportURL   https://github.com/liu620305-jpg/ocs-ai-gateway/issues
// @license      MIT
// ==/UserScript==

(function () {
  'use strict';

  const PAGE = (() => {
    try { return typeof unsafeWindow !== 'undefined' && unsafeWindow ? unsafeWindow : (typeof window !== 'undefined' ? window : {}); }
    catch (e) { return typeof window !== 'undefined' ? window : {}; }
  })();
  const HAS_GM = typeof GM_xmlhttpRequest === 'function';
  const CONFIG = (() => {
    const defaults = { gateway: 'http://127.0.0.1:8000', token: '' };
    try {
      const raw = typeof GM_getValue === 'function' ? GM_getValue('ocs_probe_config', '') : '';
      return Object.assign(defaults, raw ? JSON.parse(raw) : {});
    } catch (e) { return defaults; }
  })();
  function saveProbeConfig(patch) {
    Object.assign(CONFIG, patch || {});
    CONFIG.gateway = String(CONFIG.gateway || 'http://127.0.0.1:8000').replace(/\/+$/, '');
    try { if (typeof GM_setValue === 'function') GM_setValue('ocs_probe_config', JSON.stringify(CONFIG)); } catch (e) {}
  }
  function postProbeJSON(path, payload) {
    const url = CONFIG.gateway + path;
    const body = JSON.stringify(payload || {});
    const headers = { 'Content-Type': 'application/json' };
    if (CONFIG.token) headers['X-Gateway-Token'] = CONFIG.token;
    return new Promise((resolve, reject) => {
      if (HAS_GM) {
        GM_xmlhttpRequest({ method: 'POST', url: url, headers: headers, data: body, timeout: 10000,
          onload: (res) => { if (res.status >= 200 && res.status < 300) resolve(res); else reject(new Error('HTTP ' + res.status)); },
          onerror: () => reject(new Error('network error')),
          ontimeout: () => reject(new Error('timeout')) });
        return;
      }
      fetch(url, { method: 'POST', headers: headers, body: body, credentials: 'omit' })
        .then((res) => { if (res.ok) resolve(res); else reject(new Error('HTTP ' + res.status)); })
        .catch(reject);
    });
  }
  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('OCS 探针：设置网关/token', () => {
      const gateway = prompt('网关地址', CONFIG.gateway);
      if (gateway === null) return;
      const token = prompt('网关 token（留空表示不设置）', CONFIG.token || '');
      if (token === null) return;
      saveProbeConfig({ gateway: gateway.trim(), token: token.trim() });
    });
  }

  const ACCOUNT_SELECTORS = [
    '.u-ui-name', '.userName', '.user-name', '#userName', '.name_Chaoxing', '.personalName',
    '.ux-usr .name', '.u-usrname', '.nickname', '.username', '.m-user .name',
    '.userInfo', '.user-info', '.nav-user', '[class*="user"] .name', '[class*="userName"]'
  ];
  const COURSE_SELECTORS = [
    '.j-course-card-box .title', '.course-card-wrapper .title', '.courseTxt', '.courseName', '.course-name',
    'h1', 'h2', '.u-courseName', '.course-title', '.m-coursetitle', '.courseTitle', '.learn-title',
    '[class*="course"] [class*="name"]'
  ];

  const RATIO_PATTERNS = [
    ['已学', /已学\s*(\d+)\s*[\/／]\s*(\d+)\s*小节/],
    ['任务点', /任务点[^\d]{0,10}(\d+)\s*[\/／]\s*(\d+)/],
    ['已完成', /已完成[^\d]{0,10}(\d+)\s*[\/／]\s*(\d+)/],
    ['比值', /(\d+)\s*[\/／]\s*(\d+)\s*个?(?:任务点|课|单元|章节)/]
  ];
  const PERCENT_PATTERNS = [
    ['学习进度', /学习进度[^\d]{0,12}(\d{1,3})\s*%/],
    ['课程进度', /课程进度[^\d]{0,12}(\d{1,3})\s*%/],
    ['已完成%', /已完成\s*(\d{1,3})\s*%/]
  ];

  const text = (el) => ((el && (el.innerText || el.textContent)) || '').trim();
  const clip = (s, n) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, n || 80);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const POSITION_SELECTORS = [
    '.chapter-title', '.chapter-head', '.chapter_head', '.chapter_name', '.posCatalog_select',
    '.chapter_item', '.chapter-item', '.catalog', '.j-chapter', '.j-section', '.j-lesson',
    '.u-courselist', '.knowledge', '.unit', '.lesson', '.section', '[class*="chapter"]', '[class*="catalog"]'
  ];

  function squeeze(s) {
    return String(s || '').replace(/\s+/g, ' ').replace(/^(已学完|已完成|已学|未完成|待完成|未开始)\s*/, '').trim();
  }

  function pickCurrentNode() {
    const nodes = document.querySelectorAll('[class*="current"], [class*="active"], [class*="selected"]');
    let best = null;
    let bestScore = 0;
    nodes.forEach((node) => {
      const t = squeeze(text(node));
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
    const learn = /\/learn\/|courseware|knowledge|\/work\/|mycourse|lesson|chapter|\/mooc-ans\//i.test(location.href);
    const chapEl = document.querySelector('.u-learn-moduletitle .j-chapter, .titleBar .j-chapter, .j-chapter, .chapter-title, .chapter-head');
    const secEl = document.querySelector('.u-learn-moduletitle .j-lesson, .titleBar .j-lesson, .j-lesson, .section-title');
    if (chapEl) chapter = squeeze(text(chapEl));
    if (secEl) section = squeeze(text(secEl));
    const unitEl = document.querySelector('.j-unit.current .unit-name, .j-unit.active .unit-name, .unit-name.current, .unit-name.active');
    if (unitEl) {
      const u = squeeze(text(unitEl));
      if (u) section = section ? section + ' · ' + u : u;
    }
    const node = (chapter && section) ? null : pickCurrentNode();
    if (node) {
      section = squeeze(text(node));
      let p = node.parentElement;
      for (let i = 0; p && i < 6; i += 1, p = p.parentElement) {
        const cls = String(p.className || '').toLowerCase();
        if (!/chapter|unit|knowledge|catalog/.test(cls)) continue;
        const title = p.querySelector('.chapter-title, .chapter-head, .title, h2, h3, h4');
        const t = squeeze(title ? text(title) : '');
        if (t && t !== section && t.length <= 90) { chapter = t; break; }
      }
      if (!chapter) {
        const box = node.closest('li, .item, .unit, .lesson');
        const prev = box && box.previousElementSibling;
        const t = squeeze(prev ? text(prev) : '');
        if (t && t.length <= 90) chapter = t;
      }
    }
    if (!chapter && learn) {
      const crumb = document.querySelector('.breadcrumb, .crumbs, .position, .prev_ul, .u-crumb, #position, .m-crumb');
      if (crumb) {
        const parts = squeeze(text(crumb)).split(/[\/>»]/).map((s) => s.trim()).filter(Boolean);
        chapter = parts.length > 1 ? parts[parts.length - 1] : (parts[0] || '');
      }
    }
    if (!chapter && learn) {
      const parts = String(document.title || '').split(/[-_|]/).map((s) => s.trim())
        .filter((s) => s && !/中国大学MOOC|慕课|网易|学习通|超星|学堂在线/i.test(s));
      if (parts.length >= 3) chapter = parts[parts.length - 2] || '';
      else if (parts.length === 2) chapter = parts[1];
    }
    return { chapter: chapter.slice(0, 120), section: section.slice(0, 120), live: learn, currentNode: node ? clip(text(node), 60) : '' };
  }

  function positionCandidates() {
    const out = [];
    const seen = new Set();
    allDocs().forEach(({ doc, frame }) => {
      if (!doc) return;
      POSITION_SELECTORS.forEach((sel) => {
        let nodes = [];
        try { nodes = doc.querySelectorAll(sel); } catch (e) {}
        let n = 0;
        nodes.forEach((el) => {
          if (n >= 2 || out.length >= 24) return;
          const t = clip(text(el), 70);
          if (!t) return;
          const sig = sel + '|' + t;
          if (seen.has(sig)) return;
          seen.add(sig);
          out.push({
            sel,
            frame,
            tag: el.tagName.toLowerCase(),
            cls: clip(el.className, 70),
            text: t,
            active: /current|active|selected/.test(String(el.className || '').toLowerCase())
          });
          n += 1;
        });
      });
    });
    return out;
  }

  function allDocs() {
    const docs = [{ doc: document, frame: 'top' }];
    document.querySelectorAll('iframe').forEach((frame, i) => {
      try {
        const doc = frame.contentDocument;
        if (doc) docs.push({ doc, frame: clip(frame.id || frame.name || 'iframe' + i, 40) });
      } catch (e) {}
    });
    return docs;
  }

  function candidate(selectors) {
    const docs = allDocs();
    return selectors.map((sel) => {
      for (const { doc, frame } of docs) {
        let el = null;
        try { el = doc.querySelector(sel); } catch (e) {}
        if (el) {
          return { sel, found: true, text: clip(text(el), 60), cls: clip(String(el.className || ''), 60), frame };
        }
      }
      return { sel, found: false, text: '', cls: '', frame: '' };
    });
  }

  function structure() {
    const out = { headings: [], elements: [], frames: [] };
    allDocs().forEach(({ doc, frame }) => {
      if (!doc) return;
      let hn = 0;
      doc.querySelectorAll('h1,h2,h3,h4').forEach((el) => {
        if (hn >= 8) return;
        const t = clip(text(el), 60);
        if (!t) return;
        out.headings.push({ frame, tag: el.tagName.toLowerCase(), cls: clip(el.className, 50), text: t });
        hn += 1;
      });
      let en = 0;
      const sel = '[class*="user"],[class*="name"],[class*="course"],[class*="title"],[class*="header"],[class*="nav"],[id*="user"],[id*="course"]';
      doc.querySelectorAll(sel).forEach((el) => {
        if (en >= 24) return;
        const t = clip(text(el), 60);
        if (!t) return;
        out.elements.push({
          frame,
          tag: el.tagName.toLowerCase(),
          cls: clip(el.className, 70),
          id: clip(el.id, 40),
          text: t
        });
        en += 1;
      });
    });
    document.querySelectorAll('iframe').forEach((f) => {
      let ok = false;
      try { ok = !!f.contentDocument; } catch (e) {}
      out.frames.push({ id: clip(f.id, 40), name: clip(f.name, 40), src: clip(f.src, 120), accessible: ok });
    });
    return out;
  }

  function bestCandidate(rows) {
    const hit = rows.find((r) => r.found && r.text);
    return hit ? hit.text : '';
  }

  function scanProgress() {
    const ratios = [];
    const percents = [];
    const nodes = [];
    for (const { doc, frame } of allDocs()) {
      if (!doc) continue;
      const body = doc.body ? doc.body.innerText : '';
      RATIO_PATTERNS.forEach(([name, re]) => {
        const m = body.match(re);
        if (m) ratios.push({ name, match: clip(m[0], 50), done: Number(m[1]), total: Number(m[2]), frame });
      });
      PERCENT_PATTERNS.forEach(([name, re]) => {
        const m = body.match(re);
        if (m) percents.push({ name, match: clip(m[0], 50), percent: Number(m[1]), frame });
      });
      if (nodes.length >= 12) continue;
      const sel = '[class*="chapter"], [class*="task"], [class*="unit"], [class*="progress"], [class*="job"]';
      for (const el of doc.querySelectorAll(sel)) {
        const t = clip(text(el), 60);
        if (!t) continue;
        nodes.push({ cls: clip(String(el.className || ''), 70), text: t, frame });
        if (nodes.length >= 12) break;
      }
    }
    return { ratios, percents, nodes };
  }

  /* ---------- 网络抓取（只观察） ---------- */
  let apiHits = [];
  let pageUrl = location.href;

  function walkNumbers(obj, url) {
    const re = /(finish|complete|done|learned|total|progress|percent|rate|schedule)/i;
    const found = [];
    try {
      JSON.stringify(obj, (k, v) => {
        if (typeof v === 'number' && re.test(k)) found.push({ key: k, value: v, where: url });
        return v;
      });
    } catch (e) { return; }
    if (found.length) {
      apiHits.push({ url: clip(url, 100), fields: found.slice(0, 8) });
      apiHits = apiHits.slice(-8);
    }
  }

  const apiLog = [];
  function flagScan(obj, base, out) {
    if (!obj || typeof obj !== 'object' || out.length >= 12) return out;
    for (const [k, v] of Object.entries(obj)) {
      if (out.length >= 12) break;
      if (v && typeof v === 'object') flagScan(v, base + '.' + k, out);
      else if (/finish|complete|learn|status|progress|done|pass/i.test(k)) out.push({ path: base + '.' + k, value: v });
    }
    return out;
  }
  function logJson(url, obj) {
    try {
      apiLog.unshift({
        url: clip(String(url || ''), 150),
        size: JSON.stringify(obj || {}).length,
        keys: Object.keys(obj || {}).slice(0, 8),
        flags: flagScan(obj, '', []).slice(0, 10)
      });
      if (apiLog.length > 20) apiLog.length = 20;
    } catch (e) {}
  }

  const netLog = [];
  function logNet(method, url, ct, size) {
    const u = String(url || '');
    if (!u || u.startsWith('data:')) return;
    const key = method + ' ' + u;
    if (netLog.some((x) => x.key === key)) return;
    netLog.unshift({ key, method, url: clip(u, 160), ct: clip(ct, 40), size: size || 0 });
    if (netLog.length > 40) netLog.length = 40;
  }

  function hookNetwork() {
    try {
      if (PAGE.__ocsProbeHooked) return;
      PAGE.__ocsProbeHooked = true;
    } catch (e) {}
    const of = PAGE.fetch;
    if (typeof of === 'function') {
      try {
        PAGE.fetch = async function (...args) {
          const resp = await of.apply(this, args);
          try {
            const ct = resp.headers.get('content-type') || '';
            const method = (args[1] && args[1].method) || 'GET';
            logNet(method, String((args[0] && args[0].url) || args[0]), ct, 0);
            if (ct.includes('json')) {
              const data = await resp.clone().json();
              walkNumbers(data, String((args[0] && args[0].url) || args[0]));
              logJson((args[0] && args[0].url) || args[0], data);
            }
          } catch (e) {}
          return resp;
        };
      } catch (e) {}
    }
    const XHR = PAGE.XMLHttpRequest;
    if (!XHR || !XHR.prototype) return;
    const oo = XHR.prototype.open;
    const os = XHR.prototype.send;
    XHR.prototype.open = function (m, u, ...r) { this.__method = m; this.__url = u; return oo.call(this, m, u, ...r); };
    XHR.prototype.send = function (...a) {
      this.addEventListener('load', () => {
        try {
          const ct = this.getResponseHeader('content-type') || '';
          logNet(this.__method || 'GET', this.__url || '', ct, (this.responseText || '').length);
          if (ct.includes('json')) {
            const data = JSON.parse(this.responseText);
            walkNumbers(data, this.__url || '');
            logJson(this.__url || '', data);
          }
        } catch (e) {}
      });
      return os.apply(this, a);
    };
  }

  /* ---------- 面板 ---------- */
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;';
  document.documentElement.appendChild(host);
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `
    <style>
      :host{all:initial}
      .box{width:400px;max-height:72vh;overflow:auto;background:#0d1015;color:#e7eaf0;border:1px solid #2a3240;border-radius:10px;font:12px/1.5 "Microsoft YaHei UI","Segoe UI",sans-serif;box-shadow:0 20px 50px rgba(0,0,0,.65)}
      .head{display:flex;justify-content:space-between;align-items:center;padding:9px 12px;border-bottom:1px solid #1f252f;font-weight:600}
      .head span{color:#c6f24e;font-family:Consolas,monospace;font-size:10px;letter-spacing:1px}
      .body{padding:10px 12px}
      .sec{margin-bottom:10px}
      .sec h4{margin:0 0 4px;font-size:11px;color:#8a93a6;font-weight:600}
      .row{color:#aab3c2;word-break:break-all;margin:2px 0}
      .ok{color:#5be49b}
      .bad{color:#ff8b8b}
      .k{color:#c6f24e;font-family:Consolas,monospace}
      .foot{display:flex;gap:6px;flex-wrap:wrap;padding:9px 12px;border-top:1px solid #1f252f}
      button{font:inherit;font-size:11px;cursor:pointer;border:1px solid #2a3240;background:#151a22;color:#e7eaf0;padding:4px 8px;border-radius:5px}
      button:hover{background:#1a2029}
      button.primary{background:#c6f24e;border-color:#c6f24e;color:#0a0b0e;font-weight:600}
      .mini{width:20px;padding:2px 0;font-size:10px}
      .toast{color:#f5b544;margin-top:6px}
      .hidden{display:none}
    </style>
    <div class="box">
      <div class="head"><span>OCS 进度探针</span><button id="min" class="mini">—</button></div>
      <div id="body" class="body"></div>
      <div id="foot" class="foot">
        <button id="scan">重新扫描</button>
        <button id="hl">高亮进度区</button>
        <button id="clr">清除高亮</button>
        <button id="send" class="primary">上报测试</button>
        <button id="copy">复制诊断</button>
      </div>
    </div>`;
  const $ = (id) => root.getElementById(id);
  let minimized = false;
  let highlighted = [];

  function detectCourses() {
    const raw = [];
    document.querySelectorAll('.j-course-card-box, .course-card-wrapper').forEach((card) => {
      const title = clip(text(card.querySelector('.title')), 80);
      const progEl = card.querySelector('.course-progress-text-span') || card.querySelector('.course-progress');
      const prog = text(progEl);
      const m = prog.match(/已学\s*(\d+)\s*[\/／]\s*(\d+)\s*小节/);
      const upd = m ? null : prog.match(/已更新\s*(\d+)\s*小节/);
      if (!title || (!m && !upd)) return;
      const link = card.closest('a') || card.querySelector('a') || card;
      const href = (link && link.getAttribute && link.getAttribute('href')) || '';
      const attrs = {};
      if (link && link.getAttributeNames) {
        link.getAttributeNames().filter((n) => n.startsWith('data-')).slice(0, 6).forEach((n) => {
          attrs[n] = clip(link.getAttribute(n), 60);
        });
      }
      raw.push({
        course: title,
        done: m ? Number(m[1]) : 0,
        total: m ? Number(m[2]) : Number(upd[1]),
        text: clip(prog, 40),
        href,
        attrs
      });
    });
    const counts = {};
    raw.forEach((e) => { if (e.href) counts[e.href] = (counts[e.href] || 0) + 1; });
    const seen = new Set();
    const out = [];
    raw.forEach((e) => {
      const key = e.href && counts[e.href] === 1 ? e.href : e.course + '|' + e.total;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ course: e.course, done: e.done, total: e.total, text: e.text, key, href: e.href, attrs: e.attrs });
    });
    return out;
  }

  function unitMeta(el) {
    const chain = [];
    let p = el;
    for (let i = 0; p && i < 4; i += 1, p = p.parentElement) chain.push(clip(p.className, 60));
    const icons = [];
    el.querySelectorAll('[class*="icon"], [class*="video"], [class*="doc"], [class*="pdf"], [class*="ppt"], [class*="quiz"], [class*="discuss"], [class*="homework"], [class*="exam"]').forEach((n) => {
      const c = clip(String(n.className || ''), 50);
      if (c && !icons.includes(c)) icons.push(c);
    });
    const blob = [
      String(el.className || ''),
      ...chain,
      ...icons,
      text(el)
    ].join(' ');
    return {
      tag: el.tagName.toLowerCase(),
      cls: clip(el.className, 80),
      text: clip(text(el), 60),
      chain: chain.slice(0, 3),
      icons: icons.slice(0, 4),
      done: /完成|已学|已通过|finish|complete|done|learned|passed/i.test(blob)
    };
  }

  function detectUnits() {
    const out = [];
    const seen = new Set();
    const roots = document.querySelectorAll('.j-unitslist, .unitlist-wrapper, .j-unitlist-container, .unitslist, .j-chapter, .posCatalog_select, .chapter_item, .task-point, .sourceList, .lessonBox');
    let nodes = [];
    if (roots.length) {
      roots.forEach((root) => { nodes.push(...root.querySelectorAll('.j-unit, .lsicon, li, .unit, .unit-name')); });
    }
    if (!nodes.length) nodes = Array.from(document.querySelectorAll('.j-unit, .lsicon, .unit, .unit-name'));
    nodes.forEach((el) => {
      if (out.length >= 40) return;
      const t = clip(text(el), 60);
      if (!t) return;
      const sig = el.tagName + '|' + t;
      if (seen.has(sig)) return;
      seen.add(sig);
      out.push(unitMeta(el));
    });
    return out;
  }

  function catalogDump() {
    const box = document.querySelector('#courseLearn-inner-box, .m-learnbox');
    if (!box) return { rows: [], classes: {} };
    const rows = [];
    const seen = new Set();
    box.querySelectorAll('.titleBox, .j-name, .j-unit, li').forEach((el) => {
      if (rows.length >= 6) return;
      const row = el.closest('.titleBox') || el.parentElement || el;
      const t = clip(text(row), 90);
      if (!t || seen.has(t)) return;
      seen.add(t);
      rows.push({ text: t, html: String(row.outerHTML || '').replace(/\s+/g, ' ').slice(0, 600) });
    });
    const classes = {};
    box.querySelectorAll('*').forEach((el) => {
      String(el.className || '').split(/\s+/).forEach((c) => {
        if (!c) return;
        if (/icon|video|doc|pdf|ppt|complete|finish|learn|state|status|play|check|unit|title|task/i.test(c)) {
          classes[c] = (classes[c] || 0) + 1;
        }
      });
    });
    return {
      rows,
      classes: Object.fromEntries(Object.entries(classes).sort((a, b) => b[1] - a[1]).slice(0, 40)),
      html: String(box.innerHTML || '').replace(/\s+/g, ' ').slice(0, 15000)
    };
  }

  function diagnostic() {
    const accRows = candidate(ACCOUNT_SELECTORS);
    const courseRows = candidate(COURSE_SELECTORS);
    const progress = scanProgress();
    const courses = detectCourses();
    const ratio = progress.ratios[0];
    const first = courses[0];
    return {
      url: location.href,
      title: document.title,
      ts: new Date().toISOString(),
      accountCandidates: accRows,
      courseCandidates: courseRows,
      structure: structure(),
      courses,
      account: bestCandidate(accRows),
      course: first ? first.course : bestCandidate(courseRows),
      position: detectPosition(),
      positionCandidates: positionCandidates(),
      units: detectUnits(),
      catalog: catalogDump(),
      apiLog: apiLog.slice(0, 12),
      netLog: netLog.slice(0, 30),
      progress,
      apiHits,
      detected: first
        ? { done: first.done, total: first.total, percent: null, source: 'course-card' }
        : {
            done: ratio ? ratio.done : 0,
            total: ratio ? ratio.total : 0,
            percent: progress.percents.length ? progress.percents[0].percent : null,
            source: ratio ? 'dom-ratio' : progress.percents.length ? 'dom-percent' : apiHits.length ? 'api' : 'none'
          }
    };
  }

  function render() {
    const d = diagnostic();
    const rows = (list) => list.map((r) =>
      `<div class="row ${r.found ? 'ok' : 'bad'}"><span class="k">${esc(r.sel)}</span>${r.found && r.frame && r.frame !== 'top' ? ' [' + esc(r.frame) + ']' : ''} ${r.found ? '→ ' + esc(r.text || '（空文本）') : '✗ 未命中'}</div>`
    ).join('');
    const api = d.apiHits.length
      ? d.apiHits.map((h) => `<div class="row"><span class="k">${esc(h.url)}</span> ${h.fields.map((f) => esc(f.key) + '=' + esc(f.value)).join(', ')}</div>`).join('')
      : '<div class="row bad">暂无接口命中</div>';
    const nodes = d.progress.nodes.length
      ? d.progress.nodes.map((n) => `<div class="row"><span class="k">${esc(n.cls)}</span>${n.frame !== 'top' ? ' [' + esc(n.frame) + ']' : ''} ${esc(n.text)}</div>`).join('')
      : '<div class="row bad">没有章节/任务相关节点</div>';
    const ratios = d.progress.ratios.length
      ? d.progress.ratios.map((r) => `<div class="row ok">${esc(r.name)}: ${esc(r.match)} → ${r.done}/${r.total}${r.frame !== 'top' ? ' [' + esc(r.frame) + ']' : ''}</div>`).join('')
      : '<div class="row bad">没有比例文本</div>';
    const percents = d.progress.percents.length
      ? d.progress.percents.map((r) => `<div class="row ok">${esc(r.name)}: ${esc(r.match)} → ${r.percent}%${r.frame !== 'top' ? ' [' + esc(r.frame) + ']' : ''}</div>`).join('')
      : '<div class="row bad">没有百分比文本</div>';

    const st = d.structure || { headings: [], elements: [], frames: [] };
    const headings = st.headings.length
      ? st.headings.map((h) => `<div class="row"><span class="k">${esc(h.tag)}.${esc(h.cls)}</span>${h.frame !== 'top' ? ' [' + esc(h.frame) + ']' : ''} ${esc(h.text)}</div>`).join('')
      : '<div class="row bad">没有 h1-h4</div>';
    const elements = st.elements.length
      ? st.elements.map((e) => `<div class="row"><span class="k">${esc(e.tag)}${e.id ? '#' + esc(e.id) : ''}.${esc(e.cls)}</span>${e.frame !== 'top' ? ' [' + esc(e.frame) + ']' : ''} ${esc(e.text)}</div>`).join('')
      : '<div class="row bad">没有匹配的结构元素</div>';
    const frames = st.frames.length
      ? st.frames.map((f) => `<div class="row"><span class="k">${esc(f.id || f.name || 'iframe')}</span> ${esc(f.src)} · ${f.accessible ? '可访问' : '跨域'}</div>`).join('')
      : '<div class="row bad">没有 iframe</div>';
    const courseCards = (d.courses || []).length
      ? d.courses.map((c) => `<div class="row ok"><span class="k">${esc(c.course)}</span> ${c.done}/${c.total} · ${esc(c.text)}${c.href ? ` · <span class="k">${esc(clip(c.href, 60))}</span>` : ''}${c.attrs && Object.keys(c.attrs).length ? ' · ' + esc(Object.entries(c.attrs).map(([k, v]) => k + '=' + v).join(' ')) : ''}</div>`).join('')
      : '<div class="row bad">没有课程卡片进度</div>';
    const posCandidates = (d.positionCandidates || []).length
      ? d.positionCandidates.map((p) => `<div class="row ${p.active ? 'ok' : ''}"><span class="k">${esc(p.sel)}</span>${p.frame !== 'top' ? ' [' + esc(p.frame) + ']' : ''} ${esc(p.text)}${p.active ? ' · 高亮' : ''}</div>`).join('')
      : '<div class="row bad">没有章节候选节点</div>';
    const units = (d.units || []).length
      ? d.units.map((u) => `<div class="row ${u.done ? 'ok' : ''}"><span class="k">${esc(u.tag)}.${esc(u.cls)}</span> ${esc(u.text)}${u.icons.length ? ' · 图标:' + esc(u.icons.join(',')) : ''}${u.done ? ' · 已完成?' : ''}${u.chain.length ? ' · 父:' + esc(u.chain.join(' < ')) : ''}</div>`).join('')
      : '<div class="row bad">没有任务单元节点</div>';
    const cat = d.catalog || { rows: [], classes: {} };
    const catRows = cat.rows.length
      ? cat.rows.map((r) => `<div class="row"><span class="k">行</span> ${esc(r.text)}<div style="word-break:break-all;color:var(--faint);font-size:10px;margin-top:2px;">${esc(r.html)}</div></div>`).join('')
      : '<div class="row bad">没有目录行</div>';
    const catClasses = Object.keys(cat.classes || {}).length
      ? Object.entries(cat.classes).map(([k, v]) => `<span class="k" style="margin-right:8px">${esc(k)}×${v}</span>`).join('')
      : '<span class="bad">没有匹配的 class</span>';
    const apiRows = (d.apiLog || []).length
      ? d.apiLog.map((h) => `<div class="row"><span class="k">${esc(clip(h.url, 90))}</span> · ${h.size}B${h.flags.length ? ' · ' + h.flags.map((f) => esc(f.path) + '=' + esc(String(f.value))).join(', ') : ''}</div>`).join('')
      : '<div class="row bad">没有捕获 JSON 接口</div>';
    const netRows = (d.netLog || []).length
      ? d.netLog.map((h) => `<div class="row"><span class="k">${esc(h.method)}</span> ${esc(clip(h.url, 110))} · ${esc(h.ct || '?')} · ${h.size}B</div>`).join('')
      : '<div class="row bad">没有捕获请求</div>';

    $('body').innerHTML = `
      <div class="sec"><h4>页面</h4><div class="row">${esc(clip(location.href, 110))}</div><div class="row">${esc(clip(document.title, 80))}</div></div>
      <div class="sec"><h4>账号候选</h4>${rows(d.accountCandidates)}</div>
      <div class="sec"><h4>课程候选</h4>${rows(d.courseCandidates)}</div>
      <div class="sec"><h4>课程卡片进度（${(d.courses || []).length} 门）</h4>${courseCards}</div>
      <div class="sec"><h4>结构线索：标题</h4>${headings}</div>
      <div class="sec"><h4>结构线索：user/name/course/title</h4>${elements}</div>
      <div class="sec"><h4>iframe</h4>${frames}</div>
      <div class="sec"><h4>章节候选节点（前 24）</h4>${posCandidates}</div>
      <div class="sec"><h4>任务单元（视频/文档/讨论等，前 40）</h4>${units}</div>
      <div class="sec"><h4>课件目录（前 6 行 + HTML）</h4>${catRows}<div class="row" style="margin-top:6px">${catClasses}</div></div>
      <div class="sec"><h4>JSON 接口（含完成/学习标记的字段）</h4>${apiRows}</div>
      <div class="sec"><h4>全部请求（前 30 条）</h4>${netRows}</div>
      <div class="sec"><h4>进度：比例文本</h4>${ratios}</div>
      <div class="sec"><h4>进度：百分比文本</h4>${percents}</div>
      <div class="sec"><h4>进度相关节点（前 12）</h4>${nodes}</div>
      <div class="sec"><h4>接口命中（最近 8 条）</h4>${api}</div>
      <div class="sec"><h4>识别结果</h4><div class="row">账号：<span class="k">${esc(clip(d.account, 40)) || '—'}</span> · 课程：<span class="k">${esc(clip(d.course, 40)) || '—'}</span></div><div class="row">章节：<span class="k">${esc(clip(d.position.chapter, 50)) || '—'}</span> · 小节：<span class="k">${esc(clip(d.position.section, 50)) || '—'}</span> · 进行中：${d.position.live ? '是' : '否'}</div><div class="row">进度：<span class="k">${d.detected.total ? d.detected.done + '/' + d.detected.total : d.detected.percent != null ? d.detected.percent + '%' : '未识别'}</span>（${esc(d.detected.source)}）</div></div>
      <div id="msg" class="toast"></div>`;
  }

  function msg(t) {
    const el = $('msg');
    if (el) el.textContent = t;
  }

  function highlight() {
    clearHighlight();
    for (const { doc } of allDocs()) {
      const nodes = doc.querySelectorAll('[class*="chapter"], [class*="task"], [class*="unit"], [class*="progress"], [class*="job"]');
      for (const el of nodes) {
        const t = text(el);
        const cls = String(el.className || '');
        if (!t && !cls) continue;
        if (!/任务点|学习进度|已完成|未完成|已学|未学/.test(t) && !/chapter|task|progress|unit|job/i.test(cls)) continue;
        highlighted.push({ el, outline: el.style.outline, offset: el.style.outlineOffset });
        el.style.outline = '2px dashed #ff6b6b';
        el.style.outlineOffset = '2px';
        if (highlighted.length >= 30) return msg(`已高亮 ${highlighted.length} 个节点`);
      }
    }
    msg(`已高亮 ${highlighted.length} 个节点`);
  }

  function clearHighlight() {
    highlighted.forEach((h) => { h.el.style.outline = h.outline; h.el.style.outlineOffset = h.offset; });
    highlighted = [];
  }

  function sendOne(payload) {
    const data = Object.assign({}, payload);
    delete data.token;
    postProbeJSON('/api/progress', data)
      .then(() => msg('已上报'))
      .catch((err) => msg('上报失败：' + ((err && err.message) || '网络错误')));
  }

  function send() {
    const d = diagnostic();
    const account = d.account || '未署名';
    const pos = d.position || {};
    const list = (d.courses && d.courses.length)
      ? d.courses.map((c) => ({ account, course: c.course, key: c.key, done: c.done, total: c.total, percent: null, url: location.href, token: CONFIG.token, live: false, extra: { probe: true, source: 'course-card' } }))
      : (d.detected.total || d.detected.percent != null)
        ? [{ account, course: d.course || '未命名课程', done: d.detected.done, total: d.detected.total, percent: d.detected.percent, chapter: pos.chapter || '', section: pos.section || '', live: Boolean(pos.live), url: location.href, token: CONFIG.token, extra: { probe: true, source: d.detected.source } }]
        : null;
    if (!list) {
      msg('没有识别到进度，先看上面的候选，把诊断 JSON 复制给开发者');
      return;
    }
    list.forEach(sendOne);
    msg(`已上报 ${list.length} 条`);
  }

  async function copy() {
    const textJson = JSON.stringify(diagnostic(), null, 2);
    try {
      await navigator.clipboard.writeText(textJson);
      msg('诊断 JSON 已复制');
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = textJson;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      msg('诊断 JSON 已复制');
    }
    console.log('[OCS 探针]', textJson);
  }

  $('scan').onclick = render;
  $('hl').onclick = highlight;
  $('clr').onclick = () => { clearHighlight(); msg('已清除高亮'); };
  $('send').onclick = send;
  $('copy').onclick = copy;
  $('min').onclick = () => {
    minimized = !minimized;
    $('body').classList.toggle('hidden', minimized);
    $('foot').classList.toggle('hidden', minimized);
    $('min').textContent = minimized ? '+' : '—';
  };

  function heartbeat() {
    try {
      const ver = (typeof GM_info !== 'undefined' && GM_info.script && GM_info.script.version) || '0.4.0';
      postProbeJSON('/api/probe/heartbeat', {
        name: 'chaoxing-probe',
        ver: ver,
        url: location.href,
        extra: { title: document.title.slice(0, 80) }
      }).catch(function () {});
    } catch (e) { /* ignore */ }
  }

  hookNetwork();
  render();
  heartbeat();
  setInterval(heartbeat, 5 * 60 * 1000);
  setTimeout(render, 2500);
  setInterval(() => {
    if (location.href !== pageUrl) { pageUrl = location.href; apiHits = []; }
    if (!minimized) render();
  }, 6000);

  window.__ocsProbe = diagnostic;
})();
