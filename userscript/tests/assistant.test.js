'use strict';

const test = require('node:test');
const assert = require('node:assert');

const path = require('node:path');

const SCRIPT = path.join(__dirname, '..', '..', 'tools', 'ocs-assistant.user.js');
const assistant = require(SCRIPT);

const {
  normalizeTitle,
  parseOptionLetter,
  detectType,
  isBareLetters,
  diffOptionOrder,
  remapAnswerLetters,
  buildSnapshotPayload,
  sanitizeDataUrl,
  parseQuestion,
  parseQuestionList,
  ADAPTERS
} = assistant;

function fakeHelpers() {
  return {
    qs(el, selector) {
      return (el && el.selectors && el.selectors[selector]) || null;
    },
    qsa(el, selector) {
      const found = el && el.selectors ? el.selectors[selector] : null;
      if (!found) return [];
      return Array.isArray(found) ? found : [found];
    },
    text(el) {
      return el && el.text ? el.text : '';
    },
    attr(el, name) {
      return (el && el.attrs && el.attrs[name]) || '';
    },
    visible(el) {
      return !el || el.visible !== false;
    }
  };
}

function option(letter, text, images) {
  return {
    text: letter + '. ' + text,
    selectors: {
      '.num_option': { text: letter },
      '.after': { text: text },
      img: (images || []).map((src) => ({ attrs: { src } }))
    }
  };
}

test('normalizeTitle / parseOptionLetter', () => {
  assert.equal(normalizeTitle('  中国\u00a0 首都  '), '中国 首都');
  assert.equal(parseOptionLetter('B. 上海', 0), 'B');
  assert.equal(parseOptionLetter('C、选项', 0), 'C');
  assert.equal(parseOptionLetter('', 2), 'C');
  assert.equal(parseOptionLetter('A', 0), 'A');
});

test('detectType / isBareLetters', () => {
  assert.equal(detectType({ options: [{ text: '正确' }, { text: '错误' }] }), 'judge');
  assert.equal(detectType({ options: [{ text: '甲' }, { text: '乙' }], hasCheckbox: true }), 'multiple');
  assert.equal(detectType({ options: [], hasTextarea: true }), 'completion');
  assert.equal(detectType({ options: [{ text: '甲' }, { text: '乙' }] }), 'single');
  assert.equal(isBareLetters(['A', 'B', 'C']), true);
  assert.equal(isBareLetters(['A', '上海']), false);
  assert.equal(isBareLetters([]), false);
});

test('diffOptionOrder detects reorder but not content change', () => {
  const before = [{ letter: 'A', fp: 'i:1' }, { letter: 'B', fp: 'i:2' }];
  const after = [{ letter: 'A', fp: 'i:2' }, { letter: 'B', fp: 'i:1' }];
  const diff = diffOptionOrder(before, after);
  assert.ok(diff && diff.changed);
  assert.equal(diffOptionOrder(before, before), null);
  assert.equal(diffOptionOrder(before, [{ letter: 'A', fp: 'i:1' }, { letter: 'B', fp: 'i:3' }]), null);
});

test('remapAnswerLetters maps by fingerprint and rejects ambiguity', () => {
  const stored = [{ letter: 'A', fp: 'i:2' }];
  const current = [{ letter: 'A', fp: 'i:1' }, { letter: 'B', fp: 'i:2' }];
  assert.equal(remapAnswerLetters(stored, current), 'B');
  const duplicate = [{ letter: 'A', fp: 'i:2' }, { letter: 'B', fp: 'i:2' }];
  assert.equal(remapAnswerLetters(stored, duplicate), null);
  const missing = [{ letter: 'A', fp: 'i:9' }];
  assert.equal(remapAnswerLetters(stored, missing), null);
});

test('sanitizeDataUrl only accepts image data urls', () => {
  assert.equal(sanitizeDataUrl('data:image/png;base64,AAAA'), 'data:image/png;base64,AAAA');
  assert.equal(sanitizeDataUrl('https://example.com/a.png'), '');
  assert.equal(sanitizeDataUrl('data:text/plain;base64,AAAA'), '');
});

test('buildSnapshotPayload enforces limits', () => {
  const good = buildSnapshotPayload({
    title: '题目',
    type: 'single',
    options: [
      { letter: 'A', text: '甲' },
      { letter: 'B', image: 'data:image/png;base64,AAAA' }
    ]
  });
  assert.equal(good.ok, true);
  assert.equal(good.payload.type, 'single');
  assert.equal(good.missing, false);

  const missing = buildSnapshotPayload({
    title: '题目',
    type: 'single',
    options: [{ letter: 'A' }, { letter: 'B', text: '乙' }]
  });
  assert.equal(missing.ok, true);
  assert.equal(missing.missing, true);

  const tooMany = buildSnapshotPayload({
    title: '题目',
    type: 'single',
    options: new Array(13).fill({ text: 'x' })
  });
  assert.equal(tooMany.ok, false);

  const tooLarge = buildSnapshotPayload({
    title: '题目',
    type: 'single',
    options: [
      { letter: 'A', image: 'data:image/png;base64,' + 'A'.repeat(2000000) },
      { letter: 'B', text: '乙' }
    ]
  });
  assert.equal(tooLarge.ok, false);
});

test('parseQuestion extracts chaoxing questions', () => {
  const title = { text: '中国首都是哪里' };
  const options = [option('A', '北京'), option('B', '上海'), option('C', '广州')];
  const container = {
    selectors: {
      '.Zy_TItle .clearfix': title,
      '.Zy_ulTop li': options,
      '.analysisInfo': null
    }
  };
  const root = { selectors: { '.questionLi': [container] } };
  const parsed = parseQuestion(root, ADAPTERS.chaoxing, fakeHelpers());
  assert.ok(parsed && parsed.ok);
  assert.equal(parsed.title, '中国首都是哪里');
  assert.equal(parsed.type, 'single');
  assert.equal(parsed.options.length, 3);
  assert.deepEqual(parsed.options.map((item) => item.letter), ['A', 'B', 'C']);
  assert.deepEqual(parsed.options.map((item) => item.text), ['北京', '上海', '广州']);
});

test('parseQuestion extracts mooc questions with images', () => {
  const title = { text: '下列公式正确的是' };
  const options = [
    option('A', '', ['https://p.example/a.png']),
    option('B', '', ['https://p.example/b.png'])
  ];
  const container = {
    selectors: {
      '.j-richTxt': title,
      '.optionList li': options,
      '.analysisInfo': null
    }
  };
  const root = { selectors: { '.u-questionItem': [container] } };
  const parsed = parseQuestion(root, ADAPTERS.mooc, fakeHelpers());
  assert.ok(parsed && parsed.ok);
  assert.equal(parsed.options[0].images[0], 'https://p.example/a.png');
  assert.equal(parsed.confidence, 100);
});

test('parseQuestion treats icon-only options as judge', () => {
  const title = { text: '若事件 A 与 B 相互独立，则 A 与 B 不互斥' };
  const yes = option('A', '', []);
  yes.selectors.img = [{ attrs: { class: 'u-icon-correct', src: 'https://p.example/correct.png' } }];
  const no = option('B', '', []);
  no.selectors.img = [{ attrs: { class: 'u-icon-wrong', src: 'https://p.example/wrong.png' } }];
  const container = {
    selectors: {
      '.j-richTxt': title,
      '.optionList li': [yes, no],
      '.analysisInfo': null
    }
  };
  const root = { selectors: { '.u-questionItem': [container] } };
  const parsed = parseQuestion(root, ADAPTERS.mooc, fakeHelpers());
  assert.ok(parsed && parsed.ok);
  assert.equal(parsed.type, 'judge');
  assert.deepEqual(parsed.options.map((item) => item.text), ['正确', '错误']);
});

test('parseQuestion ignores result pages', () => {
  const container = {
    selectors: {
      '.j-richTxt': { text: '题目' },
      '.optionList li': [option('A', '甲'), option('B', '乙')],
      '.analysisInfo': { text: '正确答案：A', visible: true }
    }
  };
  const root = { selectors: { '.u-questionItem': [container] } };
  const parsed = parseQuestion(root, ADAPTERS.mooc, fakeHelpers());
  assert.ok(parsed && parsed.resultPage);
});

test('parseQuestionList dedupes same question', () => {
  const make = () => {
    const container = {
      selectors: {
        '.Zy_TItle .clearfix': { text: '同一道题' },
        '.Zy_ulTop li': [option('A', '甲'), option('B', '乙')],
        '.analysisInfo': null
      }
    };
    return container;
  };
  const root = { selectors: { '.questionLi': [make(), make()] } };
  const parsed = parseQuestionList(root, ADAPTERS.chaoxing, fakeHelpers());
  assert.equal(parsed.length, 1);
});
