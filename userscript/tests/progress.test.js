// 油猴脚本的纯函数单元测试：node --test userscript/tests/
// 跑法：node --test userscript/tests/   （Node ≥ 18，零依赖）
//
// 被测脚本在浏览器里是 IIFE，末尾在 CommonJS 环境下导出纯函数供测试，
// 因此这里只测不依赖 DOM 的解析逻辑：JSON 进度扫描、截止日期解析、URL 脱敏。

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

// sanitizeRecipeUrl 会用 location.origin 判断同源，Node 里没有 location。
globalThis.location = new URL('https://www.icourse163.org/course/abc-123');

const SCRIPT = path.join(__dirname, '..', '..', 'tools', 'ocs-progress.user.js');
const { scanJsonForProgress, parseDeadline, sanitizeRecipeUrl } = require(SCRIPT);

test('scanJsonForProgress：同一对象里的 finished/total 组成一对', () => {
  const hit = scanJsonForProgress({ finished: 3, total: 10 });
  assert.ok(hit);
  assert.equal(hit.done, 3);
  assert.equal(hit.total, 10);
  assert.equal(hit.scope, 'api');
  assert.ok(hit.confidence >= 0.7);
});

test('scanJsonForProgress：done 与 total 来自不同对象时不配对', () => {
  const hit = scanJsonForProgress({ finished: 3, other: { total: 10 } });
  // 唯一能配上的 total 必须和 finished 同对象；这里没有，故只能退化成 percent/null
  assert.ok(!hit || typeof hit.done !== 'number' || hit.total !== 10);
});

test('scanJsonForProgress：done > total 的组合被丢弃', () => {
  const hit = scanJsonForProgress({ finished: 12, total: 10 });
  assert.ok(!hit || hit.done !== 12);
});

test('scanJsonForProgress：percent 必须在 0~100 之间', () => {
  assert.equal(scanJsonForProgress({ progress: 42 }).percent, 42);
  assert.ok(!scanJsonForProgress({ progress: 420 }));
});

test('scanJsonForProgress：数字字符串也算数', () => {
  const hit = scanJsonForProgress({ completed: '4', total: '8' });
  assert.equal(hit.done, 4);
  assert.equal(hit.total, 8);
});

test('scanJsonForProgress：嵌套结构里的进度也能被找到', () => {
  const hit = scanJsonForProgress({ data: { chapter: { finished: 2, total: 5 } } });
  assert.equal(hit.done, 2);
  assert.equal(hit.total, 5);
});

test('scanJsonForProgress：完全无关的对象返回 null', () => {
  assert.equal(scanJsonForProgress({ code: 500, message: '错误' }), null);
  assert.equal(scanJsonForProgress(null), null);
  assert.equal(scanJsonForProgress('字符串'), null);
});

test('parseDeadline：带年份的完整日期', () => {
  assert.equal(parseDeadline('作业截止 2026-12-01 23:59'), '2026-12-01 23:59');
  assert.equal(parseDeadline('2026年3月5日 截止'), '2026-03-05');
});

test('parseDeadline：相对日期', () => {
  const today = new Date();
  const two = (n) => String(n).padStart(2, '0');
  const expect = (offset) => {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + offset);
    return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
  };
  assert.equal(parseDeadline('今天截止'), expect(0));
  assert.equal(parseDeadline('明天截止'), expect(1));
  assert.equal(parseDeadline('后天截止'), expect(2));
  assert.equal(parseDeadline('剩余 5 天'), expect(5));
});

test('parseDeadline：无年份且已过去 30 天以上时自动 +1 年', () => {
  const now = new Date();
  const past = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 60);
  const two = (n) => String(n).padStart(2, '0');
  const text = `${past.getMonth() + 1}月${past.getDate()}日 截止`;
  const got = parseDeadline(text);
  assert.equal(got.slice(0, 4), String(past.getFullYear() + 1));
  assert.equal(got.slice(5, 7), two(past.getMonth() + 1));
  assert.equal(got.slice(8, 10), two(past.getDate()));
});

test('parseDeadline：「截止日期」优先于「发布日期」', () => {
  // 发布日期刻意排在截止日期之后：只按「更晚的日期」排序会选错，
  // 必须靠「截止/结束」上下文加权才能选对。
  const got = parseDeadline('发布日期 2026-12-31 截止日期 2026-11-08');
  assert.equal(got, '2026-11-08');
});

test('parseDeadline：没有日期时返回空串', () => {
  assert.equal(parseDeadline('没有任何日期'), '');
  assert.equal(parseDeadline(''), '');
});

test('sanitizeRecipeUrl：同源 GET 保留并剥掉签名参数', () => {
  const got = sanitizeRecipeUrl('https://www.icourse163.org/x/api/progress?token=abc&sid=9&t=123&real=1');
  assert.ok(got);
  assert.ok(!got.includes('token='));
  assert.ok(!got.includes('sid='));
  assert.ok(got.includes('real=1'));
});

test('sanitizeRecipeUrl：跨域非课程站点直接拒绝', () => {
  assert.equal(sanitizeRecipeUrl('https://evil.example.com/api?x=1'), '');
});

test('sanitizeRecipeUrl：写接口路径被拒绝', () => {
  assert.equal(sanitizeRecipeUrl('https://www.icourse163.org/x/submit'), '');
  assert.equal(sanitizeRecipeUrl('https://www.icourse163.org/x/save/answer'), '');
});

test('sanitizeRecipeUrl：非 http(s) 协议被拒绝', () => {
  assert.equal(sanitizeRecipeUrl('javascript:alert(1)'), '');
  assert.equal(sanitizeRecipeUrl('file:///C:/x'), '');
});
