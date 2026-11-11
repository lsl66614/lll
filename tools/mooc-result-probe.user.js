// ==UserScript==
// @name         MOOC Result Probe
// @namespace    ocs-ai-gateway
// @version      0.9.1
// @description  只读抓取中国大学MOOC测验结果页/随堂测验对错；提交后自动回捞一次整卷官方答案并上报答案发布状态，发送到本地 OCS AI 网关
// @match        *://*.icourse163.org/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      127.0.0.1
// @updateURL    http://127.0.0.1:8000/tools/mooc-result-probe.meta.js
// @downloadURL  http://127.0.0.1:8000/tools/mooc-result-probe.user.js
// @connect      localhost
// @run-at       document-idle
// @homepageURL  https://github.com/liu620305-jpg/ocs-ai-gateway
// @supportURL   https://github.com/liu620305-jpg/ocs-ai-gateway/issues
// @license      MIT
// ==/UserScript==

(function () {
  'use strict';

  var CONFIG = (function () {
    var defaults = { gateway: 'http://127.0.0.1:8000', token: '' };
    try {
      var raw = typeof GM_getValue === 'function' ? GM_getValue('ocs_mooc_probe_config', '') : '';
      return Object.assign(defaults, raw ? JSON.parse(raw) : {});
    } catch (e) { return defaults; }
  })();
  CONFIG.gateway = String(CONFIG.gateway || 'http://127.0.0.1:8000').replace(/\/+$/, '');
  function saveConfig(patch) {
    Object.assign(CONFIG, patch || {});
    CONFIG.gateway = String(CONFIG.gateway || 'http://127.0.0.1:8000').replace(/\/+$/, '');
    try { if (typeof GM_setValue === 'function') GM_setValue('ocs_mooc_probe_config', JSON.stringify(CONFIG)); } catch (e) {}
  }
  var ENDPOINT = CONFIG.gateway + '/api/snapshot';
  function postJSON(url, payload) {
    if (url.indexOf('/api/') === 0) url = CONFIG.gateway + url;
    else if (url === ENDPOINT) url = CONFIG.gateway + '/api/snapshot';
    var body = JSON.stringify(payload || {});
    var headers = { 'Content-Type': 'application/json' };
    if (CONFIG.token) headers['X-Gateway-Token'] = CONFIG.token;
    return new Promise(function (resolve, reject) {
      if (typeof GM_xmlhttpRequest === 'function') {
        GM_xmlhttpRequest({ method: 'POST', url: url, headers: headers, data: body, timeout: 15000,
          onload: function (res) {
            if (res.status < 200 || res.status >= 300) { reject(new Error('HTTP ' + res.status)); return; }
            var data = null;
            try { data = JSON.parse(res.responseText || '{}'); } catch (e) {}
            resolve(data);
          },
          onerror: function () { reject(new Error('network error')); },
          ontimeout: function () { reject(new Error('timeout')); } });
        return;
      }
      fetch(url, { method: 'POST', headers: headers, body: body, credentials: 'omit' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json().catch(function () { return null; });
        })
        .then(resolve)
        .catch(reject);
    });
  }
  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('MOOC 探针：设置网关/token', function () {
      var gateway = prompt('网关地址', CONFIG.gateway);
      if (gateway === null) return;
      var token = prompt('网关 token（留空表示不设置）', CONFIG.token || '');
      if (token === null) return;
      saveConfig({ gateway: gateway.trim(), token: token.trim() });
      ENDPOINT = CONFIG.gateway + '/api/snapshot';
    });
  }
  var RESULT_PAT = /正确答案|你选对|你错选|回答正确|回答错误|我的答案|答案解析/;
  var lastSig = '';
  var lastUrl = location.href;
  var sentPing = false;
  function probeVersion() {
    try {
      if (typeof GM_info !== 'undefined' && GM_info.script && GM_info.script.version) return GM_info.script.version;
    } catch (e) {}
    return '';
  }

  function toast(msg) {
    if (!document.body) return;
    var el = document.getElementById('mooc-probe-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'mooc-probe-toast';
      el.style.cssText = 'position:fixed;right:16px;bottom:110px;z-index:2147483647;background:#111;color:#fff;padding:8px 12px;border-radius:8px;font:13px/1.4 sans-serif;opacity:.92;max-width:60vw;word-break:break-all';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.remove(); }, 5000);
  }

  /* 只收集带对错结果的题目块，不再整页回传 */
  function resultBlocks() {
    var out = [];
    function scan(doc) {
      try {
        var nodes = doc.querySelectorAll('[class*="u-questionItem"], [class*="questionItem"]');
        for (var i = 0; i < nodes.length; i++) {
          var t = nodes[i].textContent || '';
          if (RESULT_PAT.test(t)) out.push(nodes[i].outerHTML);
        }
      } catch (e) { /* ignore */ }
    }
    scan(document);
    for (var i = 0; i < window.frames.length && out.length < 200; i++) {
      try { scan(window.frames[i].document); } catch (e) { /* cross-origin */ }
    }
    return out;
  }

  function fullHtml() {
    var html = document.documentElement.outerHTML || '';
    var sep = String.fromCharCode(10);
    for (var i = 0; i < window.frames.length; i++) {
      try {
        var doc = window.frames[i].document;
        if (doc && doc.documentElement) {
          html += sep + '<!--IFRAME ' + i + ' ' + (window.frames[i].location.href || '') + '-->' + sep + doc.documentElement.outerHTML;
        }
      } catch (e) { /* cross-origin */ }
    }
    return html;
  }

  function parseIds() {
    var h = location.href;
    var tid = (h.match(/[?&#]tid=(\d+)/) || [])[1] || '';
    var aid = (h.match(/[?&#]aid=(\d+)/) || [])[1] || '';
    // 回看页的测验 id 在 id= 参数里，tid= 是课程学期 id（用它查卷会静默返回空）
    if (/quizscore/i.test(h)) {
      var quizId = (h.match(/[?&#]id=(\d+)/) || [])[1] || '';
      if (quizId) tid = quizId;
    }
    return { tid: tid, aid: aid };
  }

  /* 提交后回捞整卷官方答案：每个 (tid, aid) 只尝试一次 */
  function reportQuizStatus(ids, payload) {
    postJSON('/api/quiz/status', Object.assign({ tid: ids.tid, aid: ids.aid, url: location.href, ver: probeVersion() }, payload || {}))
      .catch(function () { /* ignore */ });
  }

  function collectQuizIds() {
    var found = new Map();
    try {
      document.querySelectorAll('a[href]').forEach(function (a) {
        var h = a.getAttribute('href') || '';
        var tid = (h.match(/[?&#]tid=(\d+)/) || [])[1];
        var aid = (h.match(/[?&#]aid=(\d+)/) || [])[1];
        if (tid && aid) found.set(tid + ':' + aid, { tid: tid, aid: aid, name: (a.textContent || '').trim().slice(0, 60) });
      });
    } catch (e) { /* ignore */ }
    return Array.from(found.values()).slice(0, 30);
  }

  function heartbeat() {
    try {
      postJSON('/api/probe/heartbeat', { name: 'mooc-result-probe', ver: probeVersion(), url: location.href })
        .catch(function () {});
    } catch (e) { /* ignore */ }
  }

  function backfillQuizzes() {
    if (!/icourse163/.test(location.host)) return;
    if (window.__ocsBackfilled) return;
    var list = collectQuizIds();
    if (!list.length) return;
    window.__ocsBackfilled = true;
    var pulled = 0;
    list.forEach(function (ids, i) {
      setTimeout(function () {
        pullOfficialAnswers(ids, true);
        pulled += 1;
      }, 600 * i);
    });
    if (list.length > 1) setTimeout(function () { toast('批量回捞测验：' + list.length + ' 个'); }, 800);
  }

  /* RPC 被 -1002/空结果挡住时的兜底：旧 DWR 通道，返回的是 DWR 自有序列化 */
  function dwrFallback(ids, say) {
    if (!ids || !ids.tid || !ids.aid) return Promise.resolve(false);
    var ts = String(Date.now());
    var sid = '';
    try {
      var m = document.cookie.match(/(?:^|; )NTESSTUDYSI=([^;]+)/);
      if (m) sid = m[1];
    } catch (e) { /* ignore */ }
    var body = [
      'callCount=1',
      'scriptSessionId=' + ts + '190',
      'httpSessionId=' + sid,
      'c0-scriptName=MocQuizBean',
      'c0-methodName=getQuizPaperDto',
      'c0-id=0',
      'c0-param0=string:' + ids.tid,
      'c0-param1=number:' + ids.aid,
      'c0-param2=boolean:false',
      'batchId=' + ts,
      'page='
    ].join(String.fromCharCode(10));
    return fetch('/dwr/call/plaincall/MocQuizBean.getQuizPaperDto.dwr', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'text/plain' },
      body: body
    }).then(function (res) { return res.text(); }).then(function (txt) {
      if (!txt || /not_auth|SecurityException/.test(txt)) return false;
      var dto = null;
      try {
        dto = new Function('return (function(){' + txt + '; return (typeof s0 !== "undefined") ? s0 : null;})()')();
      } catch (e) { return false; }
      if (!dto) return false;
      var qs = dto.objectiveQList || dto.questions || (Array.isArray(dto) ? dto : []);
      if (!qs || !qs.length) return false;
      var paper = { tid: ids.tid, aid: ids.aid, tname: dto.tname || dto.name || '', showAnalysis: true, objectiveQList: qs };
      return postJSON('/api/bank/feedback/rpc', { paper: paper, url: location.href, scene: /随堂/.test((paper.tname||'') + document.title + location.href) ? 'quick' : 'unit' })
        .then(function (res) {
          var fb = (res && res.feedback) || {};
          say('DWR 兜底回捞：解析 ' + ((res && res.parsed) || 0) + ' 题，采纳 ' + (fb.applied || 0) + ' 题');
          reportQuizStatus(ids, { published: true, pending: false, total: (res && res.parsed) || 0, dwr: true });
          return true;
        });
    }).catch(function () { return false; });
  }

  function pullOfficialAnswers(idsArg, quiet) {
    var ids = idsArg || parseIds();
    var say = quiet ? function () {} : toast;
    if (!ids.tid || !ids.aid) return;
    var key = 'ocsRpcPulled:' + ids.tid + ':' + ids.aid;
    try { if (sessionStorage.getItem(key)) return; } catch (e) { /* ignore */ }
    // 只有真的拿到答案才记账。旧版在发请求前就写标记：接口返回空、或官方还
    // 没放答案时，这一轮就再也不重试了，只能刷新页面碰运气 —— 这正是
    // 「提交了好几次，官方答案还是旧的」的来源之一。
    var markPulled = function () {
      try { sessionStorage.setItem(key, '1'); } catch (e) { /* ignore */ }
    };
    var attempt = 0;
    var csrf = '';
    try {
      var cm = document.cookie.match(/(?:^|; )NTESSTUDYSI=([^;]+)/);
      if (cm) csrf = '&csrfKey=' + encodeURIComponent(cm[1]);
    } catch (e) { /* ignore */ }
    var body = 'tid=' + encodeURIComponent(ids.tid) + '&aid=' + encodeURIComponent(ids.aid) + '&withStdAnswerAndAnalyse=1' + csrf;
    function retryLater() {
      if (attempt >= 2) return false;
      attempt += 1;
      setTimeout(callRpc, 3200 * attempt);
      return true;
    }
    function callRpc() {
      fetch('/web/j/mocQuizRpcBean.getOpenQuizPaperDto.rpc', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body
      }).then(function (res) { return res.json(); }).then(function (data) {
        // -2 多为账号级并发限制：隔 3.2 秒补一次
        if (data && data.code === -2 && retryLater()) return;
        var paper = data && data.code === 0 ? data.result : null;
        var domHasResults = resultBlocks().length > 0;
        if (!paper) {
          dwrFallback(ids, say).then(function (ok) {
            if (ok) { markPulled(); return; }
            say('官方答案接口返回空（code=' + (data && data.code) + '），稍后自动重试');
            reportQuizStatus(ids, {
              published: domHasResults,
              pending: !domHasResults,
              rpc_code: data && data.code,
              rpc_message: data && (data.message || data.code),
              dwr: false
            });
            retryLater();
          });
          return;
        }
        var list = paper.objectiveQList || paper.questions || [];
        // 试卷存在但官方还没放答案（showAnalysis=false）/ 返回 0 题，都不能
        // 当成「已回捞」：标记留着，过一会儿再问。
        var published = list.length > 0 && paper.showAnalysis !== false;
        reportQuizStatus(ids, {
          name: paper.tname || paper.name || '',
          published: published || domHasResults,
          pending: !published && !domHasResults,
          total: list.length,
          submit_status: paper.submitStatus,
          show_analysis: paper.showAnalysis
        });
        if (!published) {
          if (retryLater()) return;
          if (!domHasResults) return;
        }
        markPulled();
        if (!list.length) return;
        postJSON('/api/bank/feedback/rpc', { paper: paper, url: location.href, scene: /随堂/.test((paper.tname||'') + document.title + location.href) ? 'quick' : 'unit' })
          .then(function (res) {
            var fb = (res && res.feedback) || {};
            say('官方答案回捞：解析 ' + ((res && res.parsed) || 0) + ' 题，采纳 ' + (fb.applied || 0) + ' 题');
          }).catch(function () { say('回捞结果发送失败'); });
      }).catch(function () {
        say('官方答案接口请求失败（可能未登录或未截止）');
        reportQuizStatus(ids, { published: resultBlocks().length > 0, pending: true });
      });
    }
    callRpc();
  }

  function send(reason, quiet, html) {
    var ids = parseIds();
    var data = {
      html: html || '',
      url: location.href,
      title: document.title,
      reason: reason,
      tid: ids.tid,
      aid: ids.aid,
      ver: probeVersion()
    };
    postJSON('/api/snapshot', data).then(function () {
      if (!quiet) toast('已发送 ' + (html ? html.length : 0) + ' 字节到网关');
    }).catch(function (err) {
      if (!quiet) toast('发送失败：' + (err && err.message ? err.message : '连不上网关'));
    });
  }

  function ping() {
    if (sentPing) return;
    sentPing = true;
    send('ping', true, '');
  }

  function addButton() {
    if (document.getElementById('mooc-result-btn') || !document.body) return;
    var b = document.createElement('button');
    b.id = 'mooc-result-btn';
    b.textContent = '发送结果到网关';
    b.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#2f6bff;color:#fff;border:0;border-radius:10px;padding:10px 14px;font:13px/1.2 sans-serif;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25)';
    b.onclick = function () {
      var blocks = resultBlocks();
      if (blocks.length) {
        send('manual', false, blocks.join(String.fromCharCode(10)));
      } else {
        send('manual', false, fullHtml());
      }
    };
    document.body.appendChild(b);
  }

  function auto() {
    var blocks = resultBlocks();
    var ids = parseIds();
    var scorePage = /quizscore/i.test(location.href);
    if (!blocks.length && !(scorePage && ids.tid && ids.aid)) return;
    if (blocks.length) {
      var text = blocks.join('').replace(/<[^>]+>/g, '').replace(/\s+/g, ' ');
      var sig = location.href + '|' + blocks.length + '|' + text.length + '|' + text.slice(0, 60) + '|' + text.slice(-60);
      if (sig !== lastSig) {
        lastSig = sig;
        var reason = scorePage ? 'quizscore' : 'inline-result';
        send(reason, false, blocks.join(String.fromCharCode(10)));
      }
    }
    if (ids.tid && ids.aid) pullOfficialAnswers();
    backfillQuizzes();
  }

  function onRoute() {
    if (location.href === lastUrl) return;
    lastUrl = location.href;
    lastSig = '';
    setTimeout(auto, 2000);
  }

  setTimeout(function () {
    if (!document.body) return;
    toast('结果探针 v' + (probeVersion() || '0.9.1'));
    heartbeat();
    setInterval(heartbeat, 300000);
    ping();
    addButton();
    auto();
  }, 2000);

  setInterval(function () {
    if (!document.body) return;
    onRoute();
    addButton();
    auto();
  }, 3000);
})();
