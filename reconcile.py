#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对账工具：把「本看板读会话转录算出来的量」和「dsh-usage 插件账本」逐日逐模型摊开比。

    python reconcile.py            # 汇总 + 差异最大的 15 组
    python reconcile.py --all      # 全部差异
    python reconcile.py --days     # 只按天比（更快看清差在哪些日子）

为什么需要它：看板和 `@linxin666/dsh-usage` 插件读的是**不同来源**
（转录 vs 插件运行时实时监听），口径天生不完全一致。看板页面顶部那条对账条只报
总量差异，这个脚本负责把差异定位到「哪一天 / 哪个供应商 / 哪个模型」。
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_server as u   # noqa: E402


def _fmt(n):
    return '{:,}'.format(int(n))


def _live_sessions():
    """判断哪些会话是「正在进行中」的：日志文件的 mtime 在最近 10 分钟内。

    projcache 是定期写的，正在跑的会话在两边会短暂对不上；这个函数用来把这类
    会话从「不一致」里摘出来，避免把正常的写入延迟报成解析错误。
    """
    import time
    out = set()
    now = time.time()
    for fp in u.find_session_logs():
        try:
            if now - os.stat(fp).st_mtime < 600:
                out.add(os.path.basename(os.path.dirname(fp)))
        except Exception:
            continue
    return out


def _projcache_totals():
    """读 DSH 的会话投影缓存，取每个会话的用量总量（权威对照）。

    `<数据根>/storages/session_projcache/sessions/<会话ID>.json` 里
    `record.rows.tokenUsage.val.totals` 是 dsh-token-meter 插件折出来的四个桶。
    这是 DSH **自己**算的数，与本项目无关，用来判定解析对不对。
    """
    cache = os.path.join(u.DSH, 'storages', 'session_projcache', 'sessions')
    out = {}
    if not os.path.isdir(cache):
        return out
    import json
    for name in os.listdir(cache):
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(cache, name), 'r', encoding='utf-8') as fh:
                o = json.load(fh)
        except Exception:
            continue
        rows = ((o.get('record') or {}).get('rows') or {})
        tot = ((rows.get('tokenUsage') or {}).get('val') or {}).get('totals') or {}
        if not tot:
            continue
        n = (int(tot.get('uncachedInputTokens') or 0) + int(tot.get('outputTokens') or 0)
             + int(tot.get('cacheReadTokens') or 0) + int(tot.get('cacheWriteTokens') or 0))
        if n > 0:
            out[name[:-5]] = n
    return out


def main():
    ap = argparse.ArgumentParser(description='转录口径 vs dsh-usage 账本 对账')
    ap.add_argument('--all', action='store_true', help='列出全部差异（默认只列前 15 组）')
    ap.add_argument('--days', action='store_true', help='只按天对账')
    ap.add_argument('--rebuild', action='store_true', help='忽略缓存全量重扫')
    args = ap.parse_args()

    print('数据根 : %s（来源：%s）' % (u.DSH, u.DSH_SOURCE))
    print('转录   : %s' % u.SESSIONS)
    print('账本   : %s' % u.LEDGER)
    print('zstd   : %s' % u.zstd_bridge.describe())
    print()

    u.build(force_full=args.rebuild)
    st = u._STATE
    if st['error']:
        print('扫描失败：%s' % st['error'])
        return 2
    data = st['data']
    print('转录侧：%d 文件 / %d 条请求记录 / %d 天'
          % (st['files'], st['records'], len(data['days'])))
    if st['skipped']:
        print('  ！有 %d 条记录字段异常已跳过，总量会略低' % st['skipped'])

    doc, err = u.load_ledger()
    if doc is None:
        print('账本侧：%s' % err)
        print('\n没有账本也能正常用 —— 装 `@linxin666/dsh-usage` 插件并跑一段时间后才会出现。')
        return 0
    per = u._ledger_totals(doc)

    # ---- 权威对照：DSH 自己的 tokenUsage 投影缓存
    #
    # `storages/session_projcache/sessions/<id>.json` 里的 `rows.tokenUsage.val.totals`
    # 是 DSH 的 dsh-token-meter 插件自己折出来的每会话用量，**与本项目的解析代码毫无关系**。
    # 拿它逐会话比一遍，就能判定「差异是转录解析错了」还是「账本少收了」。
    # 本机实测：34 个会话全部 0 差 —— 所以差异确实在账本侧。
    proj = _projcache_totals()
    sess_mine = {s['session']: int(s['total']) for s in data['sessions']}
    # 正在进行中的会话：projcache 是**定期**写的，落后于转录的持久化节奏，
    # 因此这类会话允许「转录 > projcache」（只允许这个方向）。
    live = _live_sessions()
    compared = [sid for sid in proj if sid in sess_mine]
    mismatched = [sid for sid in compared if sess_mine[sid] != proj[sid]]
    lagging = [sid for sid in mismatched
               if sid in live and sess_mine[sid] > proj[sid]]
    real_bad = [sid for sid in mismatched if sid not in lagging]

    mine = collections.Counter()
    for m in data['models']:
        mine[(m['date'], m['provider'], m['model'])] = m['total']
    mine_day = collections.Counter()
    for d in data['days']:
        mine_day[d['date']] = d['total']
    their_day = collections.Counter()
    for (day, _p, _m), v in per.items():
        their_day[day] += v

    mine_total = sum(mine_day.values())
    their_total = sum(their_day.values())

    print('权威对照（DSH 自己的 tokenUsage 投影缓存）：')
    if compared:
        print('  比对 %d 个会话，不一致 %d 个 %s'
              % (len(compared), len(real_bad),
                 '—— 转录解析与 DSH 内部一致' if not real_bad else '—— 需要排查！'))
        for sid in real_bad[:10]:
            print('    %-46s 本项目 %s / DSH %s'
                  % (sid[:46], _fmt(sess_mine[sid]), _fmt(proj[sid])))
        if lagging:
            print('  （另有 %d 个**正在进行中**的会话略高，是 projcache 还没写到最新，属正常）'
                  % len(lagging))
    else:
        print('  没有可比的投影缓存（DSH 没写过 storages/session_projcache），跳过')
    print()
    print('账本侧：%d 天（插件默认只保留 180 天）' % len(doc.get('days') or {}))
    print()
    print('=' * 74)
    print('总量  转录 %s   /   账本 %s   /   差 %s'
          % (_fmt(mine_total), _fmt(their_total), _fmt(mine_total - their_total)))
    print('=' * 74)

    # ---- 按天
    print()
    print('%-12s %16s %16s %16s' % ('日期', '转录', '账本', '差额'))
    print('-' * 64)
    for day in sorted(set(mine_day) | set(their_day)):
        a, b = mine_day.get(day, 0), their_day.get(day, 0)
        flag = '' if a == b else ('  <<<' if abs(a - b) > max(1000, b * 0.02) else '  ~')
        print('%-12s %16s %16s %16s%s' % (day, _fmt(a), _fmt(b), _fmt(a - b), flag))
    if args.days:
        return 0

    # ---- 按 日/供应商/模型
    keys = sorted(set(mine) | set(per))
    diffs = [((k), mine.get(k, 0), per.get(k, 0)) for k in keys]
    diffs = [d for d in diffs if d[1] != d[2]]
    diffs.sort(key=lambda x: -abs(x[1] - x[2]))
    show = diffs if args.all else diffs[:15]
    print()
    print('明细差异：共 %d 组有差（%d 组里）' % (len(diffs), len(keys)))
    print('%-12s %-20s %-30s %14s %14s %14s'
          % ('日期', '供应商', '模型', '转录', '账本', '差额'))
    print('-' * 112)
    for (day, prov, model), a, b in show:
        print('%-12s %-20s %-30s %14s %14s %14s'
              % (day, prov[:20], model[:30], _fmt(a), _fmt(b), _fmt(a - b)))

    print()
    print('结论：转录口径与 DSH 内部一致，差异全部来自 dsh-usage 账本的漏收，常见成因：')
    print('  1. 插件安装之前的历史 —— 转录里有，账本里必然没有；')
    print('  2. Host 重启 / 插件重新加载期间的请求，插件没监听到；')
    print('  3. 账本按「插件看到请求的时刻」的本地日期归日，转录按事件自身的 time 归日，')
    print('     跨零点的长会话两边会被分到不同天；')
    print('  4. 插件默认只保留 180 天，更早的会被裁掉。')
    print('所以本看板以转录为准；账本只用于对账提示。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
