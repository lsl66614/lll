"""按语义核对官方答案与快照真值（不看字母位置）。

单选/多选：把「答案字母 → 选项内容」与快照的「正确选项内容」比。
判断题：答案是语义值（正确/错误），直接比。
选项是图片时按图片 URL 比（同一道题不同出卷版本 URL 会变 → 属正常）。
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
from app import config
from app.bank import Bank
from app.mooc_result import parse_result_html
from app.shape_key import image_urls, text_key

L = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
bank = Bank(config.BANK_PATH, config.BANK_DB_PATH)
items = bank.all()

truth = {}
for f in sorted(os.listdir(config.SNAPSHOT_DIR)):
    if not f.endswith('.html'):
        continue
    s = open(os.path.join(config.SNAPSHOT_DIR, f), encoding='utf-8', errors='replace').read()
    if 'u-questionItem' not in s:
        continue
    for it in parse_result_html(s):
        cl = it.get('correct_letters') or []
        if not cl:
            continue
        good = tuple(sorted(it['options'][L.index(c)] for c in cl
                            if L.index(c) < len(it['options'])))
        truth.setdefault((it['type'], text_key(it['title'])), set()).add(good)


def norm(text):
    """选项内容的可比形式：图片取 URL，其余取归一文字。"""
    urls = image_urls(text)
    if urls:
        return 'IMG:' + urls[0]
    return text_key(text)


ok = mismatch = 0
bad = []
for i in items:
    if i['source'] != 'official' or i['status'] != 'adopted':
        continue
    if not str(i['answer']).strip():
        continue
    key = (i['type'], text_key(i['title']))
    if key not in truth:
        continue
    cands = truth[key]
    if len(cands) != 1:
        continue
    g = list(cands)[0]
    if i['type'] == 'judge':
        # 判断题：答案是语义值
        good = i['answer'] in g
    else:
        idxs = [L.index(x) for x in str(i['answer']).replace('#', '') if x in L]
        if not idxs or any(x >= len(i['options']) for x in idxs):
            good = False
        else:
            lib = {norm(i['options'][x]) for x in idxs}
            snap = {norm(x) for x in g}
            good = bool(lib & snap)
    if good:
        ok += 1
    else:
        mismatch += 1
        bad.append((i, g))

print('官方答案与快照真值一致 %d 条，不一致 %d 条' % (ok, mismatch))
for i, g in bad[:8]:
    print()
    print('  %s ans=%r' % (i['id'], i['answer']))
    print('     库选项 =', [norm(o)[-30:] for o in i['options']])
    print('     快照真值 =', [norm(x)[-30:] for x in g])
    print('     题干 =', i['title'][:60])
