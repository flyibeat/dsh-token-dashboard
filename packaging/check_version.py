# -*- coding: utf-8 -*-
"""校验发行标签的形态：必须是 vX.Y.Z，且 X / Y / Z 各只有一位数字（0-9）。

规则来源（沿用 WorkBuddy 版项目的约定）：
    三套版本互相独立 —— 前端一个版本（dashboard.html 的 FE_VER）、后端一个版本
    （usage_server.py 的 BE_VER）、发行一个版本（git 标签 / Release）。
    发行版本每发行一次 +1，且**每位只占一位数字**：某一位到 9 就进位 ——
    所以 v1.0.9 的下一个是 v1.1.0（不是 v1.0.10），v1.9.9 的下一个是 v2.0.0。

因此本脚本**不比较「标签是否等于前后端版本号」**：发行版本本来就独立于两者，
那种比较必然报警。FE_VER / BE_VER 仍会打印出来，但**仅供参考、不参与判定**。

用法：
    python packaging/check_version.py v1.0.0
    python packaging/check_version.py             # 不传则读环境变量 GITHUB_REF_NAME

退出码：
    0  形态合法，或无法判定（没有标签 / 不是 v 开头的发行标签）
    1  形态不合法（仅在 --strict 下；CI 默认只警告，不拦发布）
"""
import os
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.dirname(HERE)

# 合法形态：v + 一位数字 + . + 一位数字 + . + 一位数字
RE_TAG_OK = re.compile(r'^v\d\.\d\.\d$')
# 每段都是纯数字、但有某段写了两位（v1.0.10）—— 用来给出更精准的提示。
RE_TAG_MULTI = re.compile(r'^v\d+\.\d+\.\d+$')


def read_ver(path, name, pattern):
    """从源码文件里抠出版本常量；读不到返回 None。"""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            text = fh.read()
    except Exception:
        return None
    m = re.search(pattern, text)
    return m.group(1) if m else None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    strict = '--strict' in sys.argv
    tag = args[0].strip() if args else (os.environ.get('GITHUB_REF_NAME') or '').strip()

    fe = read_ver(os.path.join(SRV, 'dashboard.html'), 'FE_VER',
                  r"var\s+FE_VER\s*=\s*'([^']+)'")
    be = read_ver(os.path.join(SRV, 'usage_server.py'), 'BE_VER',
                  r"BE_VER\s*=\s*os\.environ\.get\([^)]*\)\.strip\(\)\s+or\s+'([^']+)'")

    print('前端版本 FE_VER : %s' % (fe or '(读不到)'))
    print('后端版本 BE_VER : %s' % (be or '(读不到)'))
    print('发行标签        : %s' % (tag or '(无)'))
    print()

    if not tag or not tag.startswith('v'):
        print('不是 v 开头的发行标签 —— 跳过形态校验（退出码 0）。')
        return 0

    if RE_TAG_OK.match(tag):
        print('OK：标签形态合法（vX.Y.Z，每位一位数字）。')
        return 0

    if RE_TAG_MULTI.match(tag):
        parts = tag[1:].split('.')
        bad = [p for p in parts if len(p) > 1]
        print('不合法：%s 有某一位写了 %d 位数字。' % (tag, len(bad[0]) if bad else 0))
        print('本项目的规则是「每位只占一位数字，到 9 进位」：')
        print('  v1.0.9 的下一个是 v1.1.0（不是 v1.0.10）')
        print('  v1.9.9 的下一个是 v2.0.0')
    else:
        print('不合法：%s 不符合 vX.Y.Z 形态。' % tag)

    if strict:
        return 1
    print('（默认只警告，不拦发布；要拦请加 --strict）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
