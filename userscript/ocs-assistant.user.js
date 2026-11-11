// ==UserScript==
// @name         OCS 网关助手
// @namespace    ocs-ai-gateway
// @version      0.2.2
// @description  网关状态、本题速查、本页批量预取、选项顺序监测与图片选项采集（只读+上报+预取，不代答、不提交）
// @author       liu620305-jpg
// @license      MIT
// @homepage     https://github.com/liu620305-jpg/ocs-ai-gateway
// @match        *://*.chaoxing.com/*
// @match        *://*.icourse163.org/*
// @connect      127.0.0.1
// @connect      localhost
// @connect      *.nosdn.127.net
// @connect      *.127.net
// @connect      *.chaoxing.com
// @connect      *.icourse163.org
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @run-at       document-idle
// @updateURL    http://127.0.0.1:8000/tools/ocs-assistant.meta.js
// @downloadURL  http://127.0.0.1:8000/tools/ocs-assistant.user.js
// ==/UserScript==

(function () {
  'use strict';

  var PAGE = (typeof unsafeWindow !== 'undefined' && unsafeWindow)
    ? unsafeWindow
    : (typeof window !== 'undefined' ? window : globalThis);
  var SCRIPT_VERSION = (typeof GM_info !== 'undefined' && GM_info.script && GM_info.script.version) || '0.2.2';
  var LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
  var CONFIG_KEY = 'ocs_assistant_config';
  var ORDER_KEY = 'ocs_assistant_orders_v1';
  var CLIENT_KEY = 'ocs_assistant_client_id';
  var PANEL_KEY = 'ocs_assistant_panel_pos';
  var DEFAULTS = {
    gateway: 'http://127.0.0.1:8000',
    token: '',
    // 题库专业：留空表示默认专业。多课程隔离题库时在这里填对应 subject。
    subject: '',
    debug: false,
    harvest: true,
    panelCollapsed: true,
    refreshMs: 30000
  };
  var MAX_OPTIONS = 12;
  var MAX_IMAGE_BYTES = 1200000;
  var MAX_TOTAL_BYTES = 5000000;
  var HOST_ID = 'ocs-assistant-host';

  // ------------------------------------------------------------------ CORE
  function normalizeTitle(text) {
    return String(text == null ? '' : text)
      .replace(/\u00a0/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function hashString(text) {
    var h = 2166136261;
    var value = String(text || '');
    for (var i = 0; i < value.length; i++) {
      h ^= value.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0).toString(36);
  }

  function questionKey(title, type, count) {
    return hashString(normalizeTitle(title) + '|' + (type || '') + '|' + count);
  }

  function parseOptionLetter(label, index) {
    var text = String(label == null ? '' : label).trim();
    var match = text.match(/^([A-Za-z])[\s\.、)）:：]/);
    if (match) return match[1].toUpperCase();
    if (/^[A-Za-z]$/.test(text)) return text.toUpperCase();
    return index >= 0 && index < LETTERS.length ? LETTERS.charAt(index) : '';
  }

  function cleanOptionText(text) {
    var value = normalizeTitle(text);
    value = value.replace(/^[A-Za-z][\s\.、)）:：]\s*/, '');
    return value;
  }

  function optionGlyph(node, helpers) {
    // 判断题有时把 ✓/✗ 渲染成图标：选项没有文字，只有 class="u-icon-correct"
    // 或 correct.png 这类线索。识别出来回填成「正确/错误」——题型判据和快照
    // 上报用的都是同一份 text，不需要另开一条链路。
    var parts = [String(helpers.attr(node, 'class') || '')];
    ['img', 'i', 'em', 'span'].forEach(function (tag) {
      helpers.qsa(node, tag).forEach(function (el) {
        parts.push(String(helpers.attr(el, 'class') || ''));
        parts.push(String(helpers.attr(el, 'alt') || ''));
        var src = String(helpers.attr(el, 'src') || '');
        // 只看文件名，忽略 CDN 域名、目录和查询串。
        if (src) parts.push(src.split('?')[0].split('/').pop());
      });
    });
    var blob = parts.join(' ').toLowerCase();
    // 先判「错」：incorrect / untrue 里含 correct / true，反过来会误判。
    if (/(^|[^a-z])(wrong|error|false|incorrect)([^a-z]|$)|错误|不正确|不对|✗|×/.test(blob)) return '错误';
    if (/(^|[^a-z])(correct|right|true)([^a-z]|$)|正确|✓|√/.test(blob)) return '正确';
    return '';
  }

  function detectType(question) {
    if (question && question.hasTextarea) return 'completion';
    var options = (question && question.options) || [];
    var texts = options.map(function (option) { return normalizeTitle(option.text); }).filter(Boolean);
    if (texts.length === 2 && texts.every(function (text) {
      return /^(正确|错误|对|错|是|否|√|×|✓|✗)$/.test(text);
    })) return 'judge';
    if (question && question.hasCheckbox) return 'multiple';
    return 'single';
  }

  function isBareLetters(options) {
    if (!options || !options.length) return false;
    return options.every(function (option) {
      var text = typeof option === 'string' ? option : (option && option.text);
      return /^[A-Z]$/.test(String(text || '').trim());
    });
  }

  function optionFingerprint(option) {
    if (!option) return '';
    var image = String(option.image || '').trim();
    if (image) return 'i:' + hashString(image);
    return 't:' + hashString(normalizeTitle(option.text));
  }

  function diffOptionOrder(previous, current) {
    if (!previous || !current || previous.length !== current.length) return null;
    var before = previous.map(function (item) { return item.fp; }).slice().sort().join('|');
    var after = current.map(function (item) { return item.fp; }).slice().sort().join('|');
    if (!before || before !== after) return null;
    var changed = false;
    for (var i = 0; i < current.length; i++) {
      var letter = current[i].letter;
      var match = null;
      for (var j = 0; j < previous.length; j++) {
        if (previous[j].letter === letter) { match = previous[j]; break; }
      }
      if (!match || match.fp !== current[i].fp) { changed = true; break; }
    }
    if (!changed) return null;
    return {
      changed: true,
      previous: previous.map(function (item) { return item.letter + '=' + item.fp; }),
      current: current.map(function (item) { return item.letter + '=' + item.fp; })
    };
  }

  function remapAnswerLetters(stored, currentOptions) {
    if (!stored || !currentOptions) return null;
    var used = {};
    var letters = [];
    for (var i = 0; i < stored.length; i++) {
      var matches = [];
      for (var j = 0; j < currentOptions.length; j++) {
        if (currentOptions[j].fp === stored[i].fp) matches.push(j);
      }
      if (matches.length !== 1 || used[matches[0]]) return null;
      used[matches[0]] = true;
      letters.push(currentOptions[matches[0]].letter);
    }
    return letters.sort().join('#');
  }

  function sanitizeDataUrl(value) {
    var text = String(value || '').trim();
    if (!/^data:image\/(png|jpeg|jpg|webp);base64,/i.test(text)) return '';
    return /^data:image\/(png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=\s]+$/i.test(text) ? text : '';
  }

  function estimateDataUrlBytes(value) {
    var text = String(value || '');
    var comma = text.indexOf(',');
    if (comma < 0) return 0;
    return Math.floor((text.length - comma - 1) * 3 / 4);
  }

  function buildSnapshotPayload(question, limits) {
    var options = (question && question.options) || [];
    var maxOptions = (limits && limits.maxOptions) || MAX_OPTIONS;
    var maxImage = (limits && limits.maxImageBytes) || MAX_IMAGE_BYTES;
    var maxTotal = (limits && limits.maxTotalBytes) || MAX_TOTAL_BYTES;
    if (!question || !normalizeTitle(question.title)) return { ok: false, reason: '没有题干' };
    if (!options.length || options.length > maxOptions) return { ok: false, reason: '选项数量不支持' };
    var total = 0;
    var payloadOptions = [];
    var missing = false;
    for (var i = 0; i < options.length; i++) {
      var option = options[i];
      var image = sanitizeDataUrl(option.image);
      var size = image ? estimateDataUrlBytes(image) : 0;
      if (size > maxImage) return { ok: false, reason: '选项图过大' };
      total += size;
      var text = normalizeTitle(option.text);
      if (!image && !text) missing = true;
      payloadOptions.push({
        letter: option.letter || LETTERS.charAt(i),
        text: text,
        image: image
      });
    }
    if (total > maxTotal) return { ok: false, reason: '选项图总大小超限' };
    if (!payloadOptions.some(function (option) { return option.image || option.text; })) {
      return { ok: false, reason: '没有选项内容' };
    }
    return {
      ok: true,
      missing: missing,
      payload: {
        title: normalizeTitle(question.title),
        type: question.type || 'single',
        options: payloadOptions,
        subject: question.subject || '',
        url: question.url || '',
        order_fp: question.orderFp || '',
        pending_images: missing
      }
    };
  }

  // ------------------------------------------------------------------ ADAPTERS
  var ADAPTERS = {
    chaoxing: {
      name: 'chaoxing',
      containers: ['.questionLi', '.TiMu', '.Cy_TItle', '[class*="question-item"]'],
      title: ['.Zy_TItle .clearfix', '.Zy_TItle', '.Cy_TItle', '.mark_name', '.stem_answer'],
      options: ['.Zy_ulTop li', '.answerList li', '.stem_answer li', 'ul li'],
      letter: ['.num_option', '.optionPos', 'input[type="radio"]', 'input[type="checkbox"]'],
      body: ['.after', '.optionText', 'label', '.fl.after'],
      checkbox: 'input[type="checkbox"]',
      textarea: 'textarea, .inputtxt'
    },
    mooc: {
      name: 'mooc',
      containers: ['.u-questionItem', '.j-questionItem', '[class*="questionItem"]'],
      title: ['.j-richTxt', '.richTxt', '.question-title', '.q-title'],
      options: ['.optionList li', 'ul.optionList li', 'li[class*="option"]', 'ul li'],
      letter: ['.optionPos', '.option-letter'],
      body: ['.optionCnt', '.option-content', '.optionText'],
      checkbox: 'input[type="checkbox"]',
      textarea: 'textarea, .inputtxt, .j-textarea'
    },
    generic: {
      name: 'generic',
      containers: ['[class*="question-item"]', '[class*="questionItem"]', '.question', '.subject'],
      title: ['.title', '.stem', '.question-title', '.q-title', 'h3'],
      options: ['ul li', '.option', '.answer-item'],
      letter: ['.option-letter', '.num_option', 'input[type="radio"]', 'input[type="checkbox"]'],
      body: ['.option-content', '.after', '.optionText', 'label'],
      checkbox: 'input[type="checkbox"]',
      textarea: 'textarea, .inputtxt'
    }
  };

  function adapterFor(host) {
    var name = String(host || (typeof location !== 'undefined' ? location.hostname : ''));
    if (/icourse163/.test(name)) return ADAPTERS.mooc;
    if (/chaoxing/.test(name)) return ADAPTERS.chaoxing;
    return ADAPTERS.generic;
  }

  function domHelpers() {
    return {
      qs: function (el, selector) { return el && el.querySelector ? el.querySelector(selector) : null; },
      qsa: function (el, selector) {
        if (!el || !el.querySelectorAll) return [];
        return Array.prototype.slice.call(el.querySelectorAll(selector));
      },
      text: function (el) { return el && el.textContent ? el.textContent : ''; },
      attr: function (el, name) { return el && el.getAttribute ? el.getAttribute(name) : ''; },
      visible: function (el) {
        if (!el) return false;
        if (el.hidden) return false;
        if (el.classList && el.classList.contains('f-dn')) return false;
        if (typeof getComputedStyle === 'function') {
          var style = getComputedStyle(el);
          return style.display !== 'none' && style.visibility !== 'hidden';
        }
        return true;
      }
    };
  }

  function firstMatch(container, selectors, helpers) {
    for (var i = 0; i < selectors.length; i++) {
      var found = helpers.qs(container, selectors[i]);
      if (found) return { el: found, selector: selectors[i] };
    }
    return { el: null, selector: '' };
  }

  function parseContainer(container, adapter, helpers) {
    var analysis = helpers.qs(container, '.analysisInfo');
    if (analysis && helpers.visible(analysis)) {
      return { resultPage: true, container: container, adapter: adapter.name };
    }
    var titleMatch = firstMatch(container, adapter.title, helpers);
    var title = normalizeTitle(titleMatch.el ? helpers.text(titleMatch.el) : '');
    if (!title) {
      var fallback = helpers.qs(container, '.mark_name, .question-title, .q-title');
      title = normalizeTitle(fallback ? helpers.text(fallback) : helpers.text(container).slice(0, 200));
    }
    var hasTextarea = Boolean(helpers.qs(container, adapter.textarea));
    var hasCheckbox = Boolean(helpers.qs(container, adapter.checkbox));
    var optionNodes = [];
    for (var i = 0; i < adapter.options.length; i++) {
      optionNodes = helpers.qsa(container, adapter.options[i]);
      if (optionNodes.length >= 2) break;
    }
    if (!optionNodes.length) return null;

    var options = [];
    for (var index = 0; index < optionNodes.length; index++) {
      var node = optionNodes[index];
      var letterEl = null;
      for (var j = 0; j < adapter.letter.length; j++) {
        letterEl = helpers.qs(node, adapter.letter[j]);
        if (letterEl) break;
      }
      var letter = '';
      if (letterEl) {
        letter = parseOptionLetter(helpers.attr(letterEl, 'value') || helpers.text(letterEl), index);
      }
      if (!letter) letter = LETTERS.charAt(index);
      var bodyEl = null;
      for (var k = 0; k < adapter.body.length; k++) {
        bodyEl = helpers.qs(node, adapter.body[k]);
        if (bodyEl) break;
      }
      var text = cleanOptionText(bodyEl ? helpers.text(bodyEl) : helpers.text(node));
      if (letter && text.indexOf(letter + '.') === 0) text = cleanOptionText(text);
      var images = helpers.qsa(node, 'img').map(function (img) {
        return helpers.attr(img, 'src') || helpers.attr(img, 'data-src') || '';
      }).filter(Boolean);
      var glyph = optionGlyph(node, helpers);
      // 图标选项没有正文，用识别出的「正确/错误」当文本：detectType 的
      // 文本判据、选项指纹、快照上报都直接受益。
      if (!text && glyph) text = glyph;
      options.push({ letter: letter, text: text, images: images, image: images[0] || '', glyph: glyph });
    }
    var type = detectType({ options: options, hasCheckbox: hasCheckbox, hasTextarea: hasTextarea });
    var filled = options.filter(function (option) { return option.text || option.images.length; }).length;
    return {
      ok: true,
      title: title,
      type: type,
      options: options,
      hasCheckbox: hasCheckbox,
      hasTextarea: hasTextarea,
      confidence: Math.round(100 * filled / Math.max(1, options.length)),
      container: container,
      adapter: adapter.name,
      selectors: { title: titleMatch.selector, options: optionNodes.length }
    };
  }

  function parseQuestion(root, adapter, helpers) {
    helpers = helpers || domHelpers();
    adapter = adapter || ADAPTERS.generic;
    var containers = [];
    for (var i = 0; i < adapter.containers.length; i++) {
      containers = helpers.qsa(root, adapter.containers[i]);
      if (containers.length) break;
    }
    for (var j = 0; j < containers.length; j++) {
      var parsed = parseContainer(containers[j], adapter, helpers);
      if (parsed && parsed.ok) return parsed;
      if (parsed && parsed.resultPage) return parsed;
    }
    return null;
  }

  function parseQuestionList(root, adapter, helpers) {
    helpers = helpers || domHelpers();
    adapter = adapter || ADAPTERS.generic;
    var containers = [];
    for (var i = 0; i < adapter.containers.length; i++) {
      containers = helpers.qsa(root, adapter.containers[i]);
      if (containers.length) break;
    }
    var out = [];
    var seen = {};
    for (var j = 0; j < containers.length; j++) {
      var parsed = parseContainer(containers[j], adapter, helpers);
      if (!parsed || !parsed.ok) continue;
      var key = questionKey(parsed.title, parsed.type, parsed.options.length);
      if (seen[key]) continue;
      seen[key] = true;
      out.push(parsed);
    }
    return out;
  }

  // ------------------------------------------------------------------ 网络
  function gmValue(key, fallback) {
    try {
      if (typeof GM_getValue === 'function') {
        var value = GM_getValue(key, fallback);
        return value === undefined || value === null ? fallback : value;
      }
    } catch (error) { /* 忽略 */ }
    return fallback;
  }

  function gmSet(key, value) {
    try {
      if (typeof GM_setValue === 'function') GM_setValue(key, value);
    } catch (error) { /* 忽略 */ }
  }

  function getConfig() {
    var raw = gmValue(CONFIG_KEY, null);
    var cfg = {};
    if (typeof raw === 'string' && raw) {
      try { cfg = JSON.parse(raw); } catch (error) { cfg = {}; }
    } else if (raw && typeof raw === 'object') {
      cfg = raw;
    }
    var merged = {};
    Object.keys(DEFAULTS).forEach(function (key) {
      merged[key] = Object.prototype.hasOwnProperty.call(cfg, key) ? cfg[key] : DEFAULTS[key];
    });
    merged.gateway = String(merged.gateway || DEFAULTS.gateway).replace(/\/+$/, '');
    merged.token = String(merged.token || '');
    merged.subject = String(merged.subject || '').trim();
    merged.refreshMs = Math.max(10000, Math.min(300000, Number(merged.refreshMs) || DEFAULTS.refreshMs));
    merged.harvest = Boolean(merged.harvest);
    merged.debug = Boolean(merged.debug);
    return merged;
  }

  function saveConfig(cfg) {
    gmSet(CONFIG_KEY, JSON.stringify(cfg));
  }

  function clientId() {
    var id = String(gmValue(CLIENT_KEY, '') || '');
    if (!id) {
      id = 'ocsa_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      gmSet(CLIENT_KEY, id);
    }
    return id;
  }

  function request(method, path, payload, opts) {
    var timeout = (opts && opts.timeout) || 12000;
    var cfg = getConfig();
    var url = cfg.gateway + path;
    var headers = { 'Content-Type': 'application/json' };
    if (cfg.token) headers['X-Gateway-Token'] = cfg.token;
    var body = payload === undefined || payload === null ? undefined : JSON.stringify(payload);
    return new Promise(function (resolve) {
      if (typeof GM_xmlhttpRequest === 'function') {
        GM_xmlhttpRequest({
          method: method,
          url: url,
          headers: headers,
          data: body,
          timeout: timeout,
          onload: function (response) {
            var data = null;
            try { data = JSON.parse(response.responseText); } catch (error) { data = null; }
            resolve({ ok: response.status >= 200 && response.status < 300, status: response.status, data: data });
          },
          onerror: function () { resolve({ ok: false, status: 0, error: 'network' }); },
          ontimeout: function () { resolve({ ok: false, status: 0, error: 'timeout' }); }
        });
        return;
      }
      fetch(url, {
        method: method,
        headers: headers,
        body: body,
        credentials: 'omit'
      }).then(function (response) {
        return response.text().then(function (text) {
          var data = null;
          try { data = JSON.parse(text); } catch (error) { data = null; }
          resolve({ ok: response.ok, status: response.status, data: data });
        });
      }).catch(function () { resolve({ ok: false, status: 0, error: 'network' }); });
    });
  }

  function fetchImageAsDataUrl(url) {
    if (!url) return Promise.resolve('');
    if (/^data:image\//i.test(url)) return Promise.resolve(sanitizeDataUrl(url));
    return new Promise(function (resolve) {
      if (typeof GM_xmlhttpRequest === 'function') {
        GM_xmlhttpRequest({
          method: 'GET',
          url: url,
          responseType: 'blob',
          timeout: 12000,
          onload: function (response) {
            if (response.status >= 200 && response.status < 300 && response.response) {
              var reader = new FileReader();
              reader.onload = function () { resolve(sanitizeDataUrl(reader.result)); };
              reader.onerror = function () { resolve(''); };
              reader.readAsDataURL(response.response);
            } else {
              resolve('');
            }
          },
          onerror: function () { resolve(''); },
          ontimeout: function () { resolve(''); }
        });
        return;
      }
      fetch(url, { credentials: 'include' }).then(function (response) {
        return response.blob();
      }).then(function (blob) {
        var reader = new FileReader();
        reader.onload = function () { resolve(sanitizeDataUrl(reader.result)); };
        reader.onerror = function () { resolve(''); };
        reader.readAsDataURL(blob);
      }).catch(function () { resolve(''); });
    });
  }

  // ------------------------------------------------------------------ 状态
  var state = {
    cfg: null,
    health: null,
    detail: null,
    versions: null,
    probes: null,
    questions: [],
    current: null,
    lookup: null,
    snapshot: null,
    vision: null,
    prefetch: null,
    conflicts: [],
    error: '',
    posted: {},
    orders: {}
  };

  function loadOrders() {
    var raw = gmValue(ORDER_KEY, null);
    if (typeof raw === 'string' && raw) {
      try { state.orders = JSON.parse(raw) || {}; } catch (error) { state.orders = {}; }
    } else if (raw && typeof raw === 'object') {
      state.orders = raw;
    }
  }

  function saveOrders() {
    var keys = Object.keys(state.orders);
    if (keys.length > 300) {
      keys.sort(function (a, b) {
        return Number(state.orders[a].at || 0) - Number(state.orders[b].at || 0);
      });
      keys.slice(0, keys.length - 300).forEach(function (key) { delete state.orders[key]; });
    }
    gmSet(ORDER_KEY, JSON.stringify(state.orders));
  }

  function orderOf(question) {
    return question.options.map(function (option) {
      return { letter: option.letter, fp: optionFingerprint(option) };
    });
  }

  function monitorOrder(question) {
    var key = questionKey(question.title, question.type, question.options.length);
    var current = orderOf(question);
    var previous = state.orders[key];
    state.orders[key] = { order: current, at: Date.now() };
    saveOrders();
    if (!previous || !previous.order) return null;
    var diff = diffOptionOrder(previous.order, current);
    if (diff) {
      toast('选项顺序已变化：' + question.title.slice(0, 30));
      return diff;
    }
    return null;
  }

  // ------------------------------------------------------------------ 面板
  var ui = { root: null, shadow: null };

  function el(tag, props, children) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        if (key === 'text') node.textContent = props[key];
        else if (key === 'style') node.setAttribute('style', props[key]);
        else node.setAttribute(key, props[key]);
      });
    }
    (children || []).forEach(function (child) {
      if (child) node.appendChild(child);
    });
    return node;
  }

  function toast(message) {
    if (!ui.shadow) return;
    var node = ui.shadow.getElementById('ocsa-toast');
    if (!node) {
      node = el('div', { id: 'ocsa-toast' });
      ui.shadow.querySelector('.wrap').appendChild(node);
    }
    node.textContent = message;
    node.style.opacity = '1';
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { node.style.opacity = '0'; }, 3500);
  }

  function createPanel() {
    if (document.getElementById(HOST_ID)) return;
    var host = el('div', { id: HOST_ID });
    host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;';
    var shadow = host.attachShadow({ mode: 'open' });
    var style = el('style');
    style.textContent = [
      ':host{all:initial}',
      '.wrap{font:12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#e8eaf0;}',
      '.card{width:320px;max-height:70vh;overflow:auto;background:#171923;border:1px solid #2d3244;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);}',
      '.head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid #2d3244;cursor:move;user-select:none;}',
      '.dot{width:8px;height:8px;border-radius:50%;background:#6b7280;flex:0 0 auto;}',
      '.dot.ok{background:#22c55e}.dot.bad{background:#ef4444}.dot.warn{background:#f59e0b}',
      '.head b{font-size:13px;font-weight:600}.ver{color:#8b93a7;font-size:11px;margin-left:auto}',
      '.head button{background:none;border:0;color:#8b93a7;font-size:16px;cursor:pointer;padding:0 4px}',
      '.body{padding:10px 12px}.collapsed .body{display:none}',
      '.row{display:flex;gap:8px;justify-content:space-between;padding:2px 0}',
      '.muted{color:#8b93a7}.ok{color:#22c55e}.bad{color:#ef4444}.warn{color:#f59e0b}',
      '.section{margin:10px 0 6px;font-weight:600;color:#c7ccda}',
      '.qtitle{color:#f3f4f8;word-break:break-word;max-height:64px;overflow:auto}',
      '.opt{display:flex;gap:6px;padding:2px 0;color:#aeb5c6;word-break:break-all}',
      '.opt b{color:#dbe0ee;min-width:14px}',
      'input{width:100%;box-sizing:border-box;background:#0f1118;border:1px solid #2d3244;color:#e8eaf0;border-radius:8px;padding:6px 8px;margin:4px 0}',
      'button.act{background:#2f6feb;border:0;color:#fff;border-radius:8px;padding:6px 10px;cursor:pointer;font-size:12px;margin:2px 6px 2px 0}',
      'button.ghost{background:#242938;color:#c7ccda}',
      'pre{white-space:pre-wrap;word-break:break-all;background:#0f1118;border:1px solid #2d3244;border-radius:8px;padding:8px;max-height:180px;overflow:auto;font-size:11px;color:#b9c0d0}',
      '#ocsa-toast{position:absolute;left:0;right:0;bottom:8px;margin:0 12px;background:#2b3245;color:#fff;padding:6px 10px;border-radius:8px;opacity:0;transition:opacity .2s;text-align:center}',
      '.hidden{display:none}'
    ].join('');
    var wrap = el('div', { class: 'wrap' });
    var card = el('div', { class: 'card' });
    var dot = el('span', { class: 'dot', id: 'ocsa-dot' });
    var title = el('b', { text: '网关助手' });
    var ver = el('span', { class: 'ver', text: SCRIPT_VERSION });
    var min = el('button', { id: 'ocsa-min', title: '最小化', text: '—' });
    var head = el('div', { class: 'head' }, [dot, title, ver, min]);
    var body = el('div', { class: 'body' }, [
      el('div', { class: 'row' }, [el('span', { id: 'ocsa-online', class: 'muted', text: '未连接' })]),
      el('div', { class: 'row' }, [el('span', { class: 'muted', text: '题库' }), el('span', { id: 'ocsa-bank', text: '-' })]),
      el('div', { class: 'row' }, [el('span', { class: 'muted', text: '待确认 / 冲突' }), el('span', { id: 'ocsa-bank2', text: '-' })]),
      el('div', { class: 'row' }, [el('span', { class: 'muted', text: '脚本版本' }), el('span', { id: 'ocsa-ver', text: SCRIPT_VERSION })]),
      el('div', { class: 'row' }, [el('span', { class: 'muted', text: '本题' }), el('span', { id: 'ocsa-qstate', text: '-' })]),
      el('div', { class: 'section', id: 'ocsa-section', text: '本题' }),
      el('div', { class: 'qtitle', id: 'ocsa-title', text: '扫描中…' }),
      el('div', { id: 'ocsa-optlist' }),
      el('div', { id: 'ocsa-actions' }),
      el('input', { id: 'ocsa-answer', placeholder: '人工答案（如 B 或 A#C）' }),
      el('div', { id: 'ocsa-lookup', class: 'muted' }),
      el('div', { id: 'ocsa-vision', class: 'muted' }),
      el('div', { class: 'section', text: '批量' }),
      el('div', { class: 'row' }, [
        el('button', { class: 'act ghost', id: 'ocsa-prefetchbtn', text: '本页预取' }),
        el('span', { id: 'ocsa-prefetchout', class: 'muted', text: '未预取' })
      ]),
      el('div', { class: 'section', text: '诊断' }),
      el('div', { class: 'row' }, [
        el('button', { class: 'act ghost', id: 'ocsa-rescan', text: '重新扫描' }),
        el('button', { class: 'act ghost', id: 'ocsa-refresh', text: '刷新状态' }),
        el('button', { class: 'act ghost', id: 'ocsa-debug', text: '复制诊断' })
      ]),
      el('pre', { id: 'ocsa-debugout', class: 'hidden' })
    ]);
    card.appendChild(head);
    card.appendChild(body);
    wrap.appendChild(card);
    shadow.appendChild(style);
    shadow.appendChild(wrap);
    document.documentElement.appendChild(host);
    ui.root = host;
    ui.shadow = shadow;
    if (getConfig().panelCollapsed) card.classList.add('collapsed');
    min.addEventListener('click', function () {
      card.classList.toggle('collapsed');
      var cfg = getConfig();
      cfg.panelCollapsed = card.classList.contains('collapsed');
      saveConfig(cfg);
    });
    shadow.getElementById('ocsa-rescan').addEventListener('click', function () { scan(true); });
    shadow.getElementById('ocsa-refresh').addEventListener('click', function () { refreshStatus(); });
    shadow.getElementById('ocsa-debug').addEventListener('click', function () {
      var text = JSON.stringify(diagnostic(), null, 2);
      shadow.getElementById('ocsa-debugout').textContent = text;
      shadow.getElementById('ocsa-debugout').classList.remove('hidden');
      try { navigator.clipboard.writeText(text); toast('诊断已复制'); } catch (error) { toast('诊断已输出'); }
    });
    shadow.getElementById('ocsa-prefetchbtn').addEventListener('click', function () { prefetchPage(); });
    var answer = shadow.getElementById('ocsa-answer');
    var actions = shadow.getElementById('ocsa-actions');
    actions.appendChild(el('button', { class: 'act', id: 'ocsa-lookupbtn', text: '查题库' }));
    actions.appendChild(el('button', { class: 'act ghost', id: 'ocsa-harvestbtn', text: '上报选项图' }));
    actions.appendChild(el('button', { class: 'act ghost', id: 'ocsa-solvebtn', text: '识图作答' }));
    actions.appendChild(el('button', { class: 'act ghost', id: 'ocsa-savebtn', text: '保存人工答案' }));
    shadow.getElementById('ocsa-lookupbtn').addEventListener('click', function () {
      if (state.current) lookupQuestion(state.current);
    });
    shadow.getElementById('ocsa-harvestbtn').addEventListener('click', function () {
      if (state.current) harvestQuestion(state.current, true);
    });
    shadow.getElementById('ocsa-solvebtn').addEventListener('click', function () {
      if (state.current) solveImage(state.current, true);
    });
    shadow.getElementById('ocsa-savebtn').addEventListener('click', function () {
      if (state.current) submitCorrection(state.current, answer.value);
    });
    makeDraggable(host, head);
  }

  function makeDraggable(host, handle) {
    var pos = null;
    try { pos = JSON.parse(gmValue(PANEL_KEY, '') || 'null'); } catch (error) { pos = null; }
    if (pos && typeof pos.left === 'number') {
      host.style.left = pos.left + 'px';
      host.style.top = pos.top + 'px';
      host.style.right = 'auto';
      host.style.bottom = 'auto';
    }
    var dragging = false;
    var startX = 0;
    var startY = 0;
    var originX = 0;
    var originY = 0;
    handle.addEventListener('pointerdown', function (event) {
      dragging = true;
      startX = event.clientX;
      startY = event.clientY;
      var rect = host.getBoundingClientRect();
      originX = rect.left;
      originY = rect.top;
      handle.setPointerCapture(event.pointerId);
    });
    handle.addEventListener('pointermove', function (event) {
      if (!dragging) return;
      var left = originX + event.clientX - startX;
      var top = originY + event.clientY - startY;
      host.style.left = left + 'px';
      host.style.top = top + 'px';
      host.style.right = 'auto';
      host.style.bottom = 'auto';
    });
    handle.addEventListener('pointerup', function () {
      if (!dragging) return;
      dragging = false;
      var rect = host.getBoundingClientRect();
      gmSet(PANEL_KEY, JSON.stringify({ left: Math.round(rect.left), top: Math.round(rect.top) }));
    });
  }

  function setText(id, text, cls) {
    if (!ui.shadow) return;
    var node = ui.shadow.getElementById(id);
    if (!node) return;
    node.textContent = text;
    if (cls !== undefined) node.className = cls;
  }

  function render() {
    if (!ui.shadow) return;
    var dot = ui.shadow.getElementById('ocsa-dot');
    var online = state.health && state.health.ok;
    dot.className = 'dot ' + (online ? 'ok' : (state.error ? 'bad' : 'warn'));
    setText('ocsa-online', online ? '网关在线' : (state.error ? '连接失败：' + state.error : '未连接'),
      online ? 'ok' : (state.error ? 'bad' : 'muted'));
    var health = state.health && state.health.data ? state.health.data : {};
    setText('ocsa-bank', String(health.bank == null ? '-' : health.bank));
    var detail = state.detail && state.detail.data ? state.detail.data : null;
    if (detail && detail.bank) {
      setText('ocsa-bank2', String(detail.bank.pending || 0) + ' / ' + String(detail.bank.conflict || 0));
    } else {
      setText('ocsa-bank2', '-');
    }
    var served = state.versions && state.versions.data ? state.versions.data.served || {} : {};
    var servedVersion = served['ocs-assistant.user.js'];
    setText('ocsa-ver', SCRIPT_VERSION + (servedVersion && servedVersion !== SCRIPT_VERSION ? ' → ' + servedVersion : ''),
      servedVersion && servedVersion !== SCRIPT_VERSION ? 'warn' : 'muted');
    var pre = state.prefetch;
    var preText = '未预取';
    var preCls = 'muted';
    if (pre && pre.pending) {
      preText = '预取中… ' + pre.total + ' 题';
      preCls = 'warn';
    } else if (pre && pre.reason) {
      preText = '失败：' + pre.reason;
      preCls = 'bad';
    } else if (pre) {
      preText = '已缓存 ' + pre.cached + ' / 共 ' + pre.total;
      if (pre.skipped) preText += '（本页 ' + (pre.total + pre.skipped) + ' 题）';
      preCls = pre.failed ? 'warn' : 'ok';
    }
    setText('ocsa-prefetchout', preText, preCls);
    setText('ocsa-prefetchbtn',
      '本页预取' + (state.questions.length ? ' ' + state.questions.length + ' 题' : ''),
      'act ghost');
    var question = state.current;
    if (!question) {
      setText('ocsa-qstate', state.questions.length ? state.questions.length + ' 题' : '未识别', 'muted');
      setText('ocsa-title', '未识别到题目；可点「重新扫描」或在诊断里查看选择器');
      ui.shadow.getElementById('ocsa-optlist').textContent = '';
      return;
    }
    setText('ocsa-qstate', question.adapter + ' / ' + question.type + ' / ' + question.confidence + '%', 'muted');
    setText('ocsa-title', question.title);
    var optList = ui.shadow.getElementById('ocsa-optlist');
    optList.textContent = '';
    question.options.slice(0, 12).forEach(function (option) {
      var children = [el('b', { text: option.letter })];
      var text = option.text || (option.images.length ? '[图片选项 ' + option.images.length + ' 张]' : '[空]');
      children.push(el('span', { text: text }));
      optList.appendChild(el('div', { class: 'opt' }, children));
    });
    var lookup = state.lookup;
    var info = '';
    if (state.snapshot) {
      info = state.snapshot.ok
        ? '选项快照：已上报' + (state.snapshot.order_changed ? '（顺序已变化）' : '')
        : '选项快照：' + (state.snapshot.reason || '未上报');
    } else if (lookup && lookup.item) {
      info = '题库：' + (lookup.item.match || lookup.item.source || '命中') + ' ' + (lookup.item.answer || '');
    }
    setText('ocsa-lookup', info || '点「查题库」查看命中；图片选项点「上报选项图」');
    var vision = state.vision;
    var visionText = '';
    if (vision && vision.pending) {
      visionText = '识图中：正在请求视觉模型…';
    } else if (vision && vision.ok) {
      visionText = '识图答案：' + (vision.answer || '-') +
        (vision.provider ? ' · ' + vision.provider : '') +
        (vision.title ? ' · ' + String(vision.title).slice(0, 30) : '');
    } else if (vision && vision.reason) {
      visionText = '识图失败：' + vision.reason;
    }
    setText('ocsa-vision', visionText);
  }

  // ------------------------------------------------------------------ 功能
  function refreshStatus() {
    state.error = '';
    return request('GET', '/health').then(function (health) {
      state.health = health;
      if (!health.ok) state.error = health.error || ('HTTP ' + health.status);
      // 不再因「没填 token」跳过：网关在 GATEWAY_TOKEN 为空时本来就放行，
      // 提前 return 会让默认部署永远看不到题库数、版本漂移和探针状态。
      return request('GET', '/api/state').then(function (detail) {
        state.detail = detail;
        if (detail.status === 401) state.error = '口令不正确';
        return request('GET', '/api/tools/versions');
      }).then(function (versions) {
        state.versions = versions;
        return request('GET', '/api/probes');
      }).then(function (probes) {
        state.probes = probes;
      });
    }).catch(function () {
      state.error = 'network';
    }).then(function () {
      render();
    });
  }

  function collectQuestions() {
    var roots = [];
    try { roots.push(document); } catch (error) { /* 忽略 */ }
    try {
      for (var i = 0; i < window.frames.length && i < 10; i++) {
        try {
          if (window.frames[i].document) roots.push(window.frames[i].document);
        } catch (error) { /* 跨域 */ }
      }
    } catch (error) { /* 忽略 */ }
    var out = [];
    var seen = {};
    roots.forEach(function (root) {
      var host = '';
      try { host = root.location ? root.location.hostname : location.hostname; } catch (error) { host = location.hostname; }
      var adapter = adapterFor(host);
      parseQuestionList(root, adapter).forEach(function (question) {
        var key = questionKey(question.title, question.type, question.options.length);
        if (seen[key]) return;
        seen[key] = true;
        question.url = location.href;
        out.push(question);
      });
    });
    return out;
  }

  function scan(manual) {
    var questions = collectQuestions();
    state.questions = questions;
    var changed = false;
    questions.forEach(function (question) {
      var order = monitorOrder(question);
      if (order) changed = true;
      if (state.cfg.harvest) harvestQuestion(question, false);
    });
    var currentKey = state.current ? questionKey(state.current.title, state.current.type, state.current.options.length) : '';
    var next = questions.filter(function (question) {
      return questionKey(question.title, question.type, question.options.length) === currentKey;
    })[0] || questions[0] || null;
    state.current = next;
    if (manual) toast(questions.length ? '扫描到 ' + questions.length + ' 道题' : '没有识别到题目');
    render();
    if (changed) render();
  }

  // 批量预取：只把当前页题目丢给网关 `mode=prefetch`，不接管官方逐题流程。
  // 网关把答案写进缓存，官方脚本下一题仍然逐题请求 /ocs/search，命中后秒回。
  var PREFETCH_LIMIT = 30;   // 与 app/main.py `_BATCH_MAX_REQUEST_ITEMS` 对齐

  function batchItemsFrom(questions) {
    return questions.filter(function (question) {
      return question && question.title && question.options && question.options.length;
    }).slice(0, PREFETCH_LIMIT).map(function (question, index) {
      return {
        id: 'p' + (index + 1),
        type: question.type,
        title: question.title,
        options: question.options.map(function (option) { return option.text || ''; }),
        subject: state.cfg.subject || ''
      };
    });
  }

  function prefetchPage() {
    var questions = collectQuestions();
    state.questions = questions;
    var items = batchItemsFrom(questions);
    if (!items.length) {
      state.prefetch = { pending: false, total: 0, cached: 0, skipped: 0, reason: '没有可预取的题目' };
      toast('没有可预取的题目');
      render();
      return Promise.resolve(null);
    }
    state.prefetch = { pending: true, total: items.length, cached: 0, skipped: 0 };
    render();
    return request('POST', '/api/batch/query', { mode: 'prefetch', items: items },
      { timeout: 90000 }).then(function (result) {
      var data = result.data && result.data.data;
      if (!result.ok || !data) {
        var reason = (result.data && (result.data.detail || result.data.msg)) ||
          (result.status === 404 ? '批量接口未启用（服务端设置 BATCH_ENABLED=1）' : '') ||
          result.error || ('HTTP ' + result.status);
        state.prefetch = { pending: false, total: items.length, cached: 0, skipped: 0, reason: String(reason) };
        toast('预取失败：' + reason);
      } else {
        var stats = data.stats || {};
        state.prefetch = {
          pending: false,
          total: Number(stats.total) || items.length,
          cached: Number(stats.cached) || 0,
          answered: Number(stats.answered) || 0,
          failed: Number(stats.failed) || 0,
          skipped: questions.length - items.length,
          batchId: stats.batch_id || '',
          reason: ''
        };
        toast('预取完成：' + (state.prefetch.cached + state.prefetch.answered) + ' / ' + state.prefetch.total);
      }
      render();
      return result;
    });
  }

  function harvestQuestion(question, manual) {
    if (!question || !question.options.length) return Promise.resolve(null);
    var key = questionKey(question.title, question.type, question.options.length);
    var fp = orderOf(question).map(function (item) { return item.fp; }).join('|');
    var posted = state.posted[key];
    if (!manual && posted && posted.fp === fp && Date.now() - posted.at < 5 * 60 * 1000) {
      return Promise.resolve(null);
    }
    var images = question.options.slice(0, MAX_OPTIONS).map(function (option) {
      return option.images && option.images.length ? option.images[0] : '';
    });
    return Promise.all(images.map(fetchImageAsDataUrl)).then(function (dataUrls) {
      var enriched = {
        title: question.title,
        type: question.type,
        options: question.options.map(function (option, index) {
          return {
            letter: option.letter,
            text: option.text,
            image: dataUrls[index] || '',
            images: option.images
          };
        }),
        subject: state.cfg.subject || '',
        url: location.href
      };
      var built = buildSnapshotPayload(enriched);
      if (!built.ok) {
        state.snapshot = { ok: false, reason: built.reason };
        if (manual) toast('未上报：' + built.reason);
        render();
        return null;
      }
      return request('POST', '/api/option-snapshot', built.payload).then(function (result) {
        if (result.ok && result.data && result.data.ok) {
          state.snapshot = result.data;
          state.posted[key] = { fp: fp, at: Date.now() };
          if (result.data.order_changed) toast('选项顺序变化已上报');
        } else if (result.status === 401) {
          state.snapshot = { ok: false, reason: '口令不正确' };
        } else {
          state.snapshot = { ok: false, reason: (result.data && result.data.detail) || ('HTTP ' + result.status) };
        }
        if (manual) {
          toast(state.snapshot.ok ? '选项图已上报' : '上报失败：' + state.snapshot.reason);
        }
        render();
        return result;
      });
    });
  }

  function collectQuestionImages(question) {
    var urls = [];
    var container = question && question.container;
    if (!container || !container.querySelectorAll) return urls;
    Array.prototype.slice.call(container.querySelectorAll('img')).forEach(function (img) {
      var src = img.getAttribute('src') || img.getAttribute('data-src') || '';
      if (src && (src.indexOf('data:image/') === 0 || /^https?:/i.test(src))) urls.push(src);
    });
    return urls;
  }

  function solveImage(question, manual) {
    if (!question) return Promise.resolve(null);
    var urls = collectQuestionImages(question);
    if (!urls.length) {
      if (manual) toast('本题没有可用的图片');
      return Promise.resolve(null);
    }
    state.vision = { pending: true };
    render();
    return Promise.all(urls.slice(0, 4).map(fetchImageAsDataUrl)).then(function (dataUrls) {
      var images = dataUrls.filter(Boolean);
      if (!images.length) {
        state.vision = { ok: false, reason: '无法读取题目图片（可在 @connect 补充图片域名）' };
        if (manual) toast('识图失败：' + state.vision.reason);
        render();
        return null;
      }
      return request('POST', '/api/solve-image', { images: images, context: question.title }).then(function (result) {
        var data = result.data || {};
        if (result.ok && data.ok && data.data) {
          var parsed = data.data;
          state.vision = {
            ok: true, answer: parsed.answer, title: parsed.title,
            type: parsed.type, provider: parsed.provider
          };
          if (manual) toast('识图答案：' + (parsed.answer || '-'));
        } else {
          state.vision = { ok: false, reason: (data && data.detail) || ('HTTP ' + result.status) };
          if (manual) toast('识图失败：' + state.vision.reason);
        }
        render();
        return result;
      });
    });
  }

  function lookupQuestion(question) {
    if (!question) return Promise.resolve(null);
    var options = question.options.map(function (option) {
      return option.text || option.image || option.letter;
    });
    var payload = {
      type: question.type,
      title: question.title,
      options: options,
      subject: state.cfg.subject || ''
    };
    return request('POST', '/api/bank/query', payload).then(function (result) {
      if (result.status === 401) {
        state.lookup = { error: '口令不正确' };
      } else if (result.ok && result.data) {
        state.lookup = {
          item: result.data.item || null,
          found: Boolean(result.data.found),
          raw: result.data
        };
      } else {
        state.lookup = { error: (result.data && result.data.detail) || ('HTTP ' + result.status) };
      }
      render();
      return result;
    });
  }

  function submitCorrection(question, answer) {
    answer = String(answer || '').trim();
    if (!question || !answer) {
      toast('先填人工答案');
      return;
    }
    if (!confirm('把「' + question.title.slice(0, 40) + '」的答案保存为 ' + answer + '（来源：人工）？')) return;
    var options = question.options.map(function (option) {
      return option.text || option.image || option.letter;
    });
    var lookupItem = state.lookup && state.lookup.item;
    var body = {
      type: question.type,
      title: question.title,
      options: options,
      answer: answer,
      source: 'manual',
      status: 'adopted'
    };
    var existingId = lookupItem && lookupItem.id;
    // 更新已有记录时不传 subject：服务端 PUT 是字段合并，空值会把原专业
    // 覆盖成默认专业。只有新建题目才按当前配置写。
    if (!existingId) body.subject = state.cfg.subject || '';
    var task = existingId
      ? request('PUT', '/api/bank/' + encodeURIComponent(existingId), body)
      : request('POST', '/api/bank', body);
    task.then(function (result) {
      if (result.ok) {
        toast('已保存人工答案');
        lookupQuestion(question);
      } else {
        toast('保存失败：' + ((result.data && result.data.detail) || ('HTTP ' + result.status)));
      }
    });
  }

  function detectConflicts() {
    var conflicts = [];
    var seen = {};
    state.questions.forEach(function (question) {
      var key = normalizeTitle(question.title);
      if (seen[key]) conflicts.push('页面上有重复题目：' + key.slice(0, 30));
      seen[key] = true;
    });
    var detail = state.detail && state.detail.data;
    if (detail && detail.bank && detail.bank.conflict > 0) {
      conflicts.push('题库冲突 ' + detail.bank.conflict + ' 条');
    }
    var item = state.lookup && state.lookup.item;
    if (item && item.conflict) conflicts.push('本题在题库里标记为冲突');
    var probes = state.probes && state.probes.data && state.probes.data.items;
    if (Array.isArray(probes)) {
      var byUrl = {};
      probes.forEach(function (probe) {
        var url = String(probe.url || '');
        var name = String(probe.name || '');
        if (!url || !/ocs|考试|测验/i.test(name)) return;
        (byUrl[url] = byUrl[url] || []).push(name + ' ' + String(probe.ver || ''));
      });
      Object.keys(byUrl).forEach(function (url) {
        var versions = byUrl[url];
        if (versions.length > 1) conflicts.push('同页多个 OCS 心跳：' + versions.join(' / '));
      });
    }
    state.conflicts = conflicts;
    return conflicts;
  }

  function diagnostic() {
    return {
      version: SCRIPT_VERSION,
      url: location.href,
      adapter: adapterFor(location.hostname).name,
      questions: state.questions.map(function (question) {
        return {
          title: question.title,
          type: question.type,
          confidence: question.confidence,
          selectors: question.selectors,
          options: question.options.map(function (option) {
            return { letter: option.letter, text: option.text, images: option.images };
          })
        };
      }),
      snapshot: state.snapshot,
      lookup: state.lookup && state.lookup.raw,
      conflicts: state.conflicts,
      health: state.health && state.health.data,
      error: state.error
    };
  }

  function heartbeat() {
    request('POST', '/api/probe/heartbeat', {
      name: 'ocs-assistant',
      ver: SCRIPT_VERSION,
      url: location.href,
      client_id: clientId()
    }).catch(function () { /* 忽略 */ });
  }

  function registerMenu() {
    if (typeof GM_registerMenuCommand !== 'function') return;
    GM_registerMenuCommand('OCS 助手：设置网关/口令', function () {
      var cfg = getConfig();
      var gateway = prompt('网关地址', cfg.gateway);
      if (gateway === null) return;
      var token = prompt('管理口令（留空表示不校验）', cfg.token);
      if (token === null) return;
      cfg.gateway = String(gateway || DEFAULTS.gateway).replace(/\/+$/, '');
      cfg.token = String(token || '');
      saveConfig(cfg);
      state.cfg = cfg;
      refreshStatus();
      toast('设置已保存');
    });
    GM_registerMenuCommand('OCS 助手：设置题库专业', function () {
      var cfg = getConfig();
      var subject = prompt('题库专业（留空表示默认专业）', cfg.subject || '');
      if (subject === null) return;
      cfg.subject = String(subject || '').trim();
      saveConfig(cfg);
      state.cfg = cfg;
      refreshStatus();
      toast('专业已保存');
    });
    GM_registerMenuCommand('OCS 助手：立即刷新', function () { refreshStatus(); scan(true); });
    GM_registerMenuCommand('OCS 助手：显示/隐藏面板', function () {
      if (ui.root) ui.root.style.display = ui.root.style.display === 'none' ? '' : 'none';
    });
    GM_registerMenuCommand('OCS 助手：清空选项顺序记录', function () {
      state.orders = {};
      gmSet(ORDER_KEY, '{}');
      toast('已清空');
    });
  }

  // ------------------------------------------------------------------ 启动
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      normalizeTitle: normalizeTitle,
      hashString: hashString,
      questionKey: questionKey,
      parseOptionLetter: parseOptionLetter,
      cleanOptionText: cleanOptionText,
      detectType: detectType,
      isBareLetters: isBareLetters,
      optionFingerprint: optionFingerprint,
      diffOptionOrder: diffOptionOrder,
      remapAnswerLetters: remapAnswerLetters,
      buildSnapshotPayload: buildSnapshotPayload,
      sanitizeDataUrl: sanitizeDataUrl,
      estimateDataUrlBytes: estimateDataUrlBytes,
      parseQuestion: parseQuestion,
      parseQuestionList: parseQuestionList,
      adapterFor: adapterFor,
      ADAPTERS: ADAPTERS,
      LETTERS: LETTERS
    };
    return;
  }

  if (PAGE.__ocsAssistantBooted) return;
  PAGE.__ocsAssistantBooted = true;

  function boot() {
    state.cfg = getConfig();
    loadOrders();
    createPanel();
    registerMenu();
    refreshStatus();
    scan(false);
    heartbeat();
    setInterval(function () { refreshStatus(); }, state.cfg.refreshMs);
    setInterval(function () { scan(false); }, 5000);
    setInterval(heartbeat, 5 * 60 * 1000);
    try {
      var timer = null;
      var observer = new MutationObserver(function () {
        clearTimeout(timer);
        timer = setTimeout(function () { scan(false); }, 1000);
      });
      observer.observe(document.documentElement, { childList: true, subtree: true });
    } catch (error) { /* 忽略 */ }
    window.addEventListener('online', function () { refreshStatus(); });
    window.addEventListener('offline', function () { state.error = 'network'; render(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
