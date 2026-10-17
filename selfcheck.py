# -*- coding: utf-8 -*-
"""自检脚本：不开服务，直接验后端的关键行为。

    python selfcheck.py            # 全部检查
    python selfcheck.py --zstd     # 只验 zstd 四个后端（较慢）

覆盖：
    * 数据根定位（四级 fallback 的每一级）
    * zstd 桥：四级后端各自可用性 + 交叉一致性
    * 解析：脏记录只跳那一条、缺字段按 0、用法字段是字符串也认
    * 会话日志发现：同目录 v3+v4 只取 v4
    * 聚合：四个桶加总、inputTotal、请求数
    * 用户名/日期边界
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_server as u          # noqa: E402
import zstd_bridge                # noqa: E402

OK = 0
FAIL = 0


def check(name, cond, detail=''):
    global OK, FAIL
    if cond:
        OK += 1
        print('  ok   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s   %s' % (name, detail))


def section(title):
    print()
    print('== %s' % title)


# ------------------------------------------------------------------ 数据根
def test_data_root():
    section('数据根定位')
    got, src, _ = u._resolve_data_root()
    check('能定位到数据根', bool(got), got)
    check('数据根像 dsh（有 sessions/storages/dsh-usage）', u._looks_like_dsh(got), got)
    check('来源有说明', bool(src), src)

    # 显式指定优先
    old = os.environ.get('DSH_USAGE_HOME')
    try:
        os.environ['DSH_USAGE_HOME'] = got
        g2, s2, _ = u._resolve_data_root()
        check('DSH_USAGE_HOME 优先生效', s2.startswith('DSH_USAGE_HOME'), s2)
        check('DSH_USAGE_HOME 指向同一处', os.path.normcase(g2) == os.path.normcase(got),
              '%s != %s' % (g2, got))

        # 上级目录能自动补 .dsh
        os.environ['DSH_USAGE_HOME'] = os.path.dirname(got)
        g3, s3, _ = u._resolve_data_root()
        check('上级目录自动补 .dsh', os.path.normcase(g3) == os.path.normcase(got), s3)

        # 不存在的路径：尊重用户指定但标记出来
        os.environ['DSH_USAGE_HOME'] = os.path.join(got, '__no_such_dir__')
        g4, s4, _ = u._resolve_data_root()
        check('不存在的路径被标记', '不存在' in s4, s4)
    finally:
        if old is None:
            os.environ.pop('DSH_USAGE_HOME', None)
        else:
            os.environ['DSH_USAGE_HOME'] = old


# ------------------------------------------------------------------ 解析
def test_parse():
    section('日志解析（一坏只跳那一条）')
    lines = [
        # 正常
        json.dumps({'type': 'assistant/message', 'time': 1792248099989,
                    'data': {'usage': {'inputTokens': 100, 'outputTokens': 10,
                                       'cacheReadTokens': 900, 'cacheWriteTokens': 5},
                             'message': {'source': {'provider': 'p1', 'model': 'm1'}}}}),
        # 缺 cacheWriteTokens：按 0 处理，不跳
        json.dumps({'type': 'assistant/message', 'time': 1792248100000,
                    'data': {'usage': {'inputTokens': 1, 'outputTokens': 2,
                                       'cacheReadTokens': 3},
                             'message': {'source': {'provider': 'p1', 'model': 'm1'}}}}),
        # 脏：inputTokens 是 dict → 整条跳过并计数
        json.dumps({'type': 'assistant/message', 'time': 1792248100001,
                    'data': {'usage': {'inputTokens': {'x': 1}, 'outputTokens': 2,
                                       'cacheReadTokens': 3, 'cacheWriteTokens': 0},
                             'message': {'source': {'provider': 'p1', 'model': 'm1'}}}}),
        # 脏：time 是字符串 → 跳过
        json.dumps({'type': 'assistant/message', 'time': 'nope',
                    'data': {'usage': {'inputTokens': 1, 'outputTokens': 2,
                                       'cacheReadTokens': 3, 'cacheWriteTokens': 0},
                             'message': {'source': {'provider': 'p1', 'model': 'm1'}}}}),
        # 没有 usage 的事件：忽略，不算脏
        json.dumps({'type': 'tool/result', 'time': 1792248100002, 'data': {}}),
        # 没有 source：用「未知供应商/模型」兜底，仍计入
        json.dumps({'type': 'assistant/message', 'time': 1792248100003,
                    'data': {'usage': {'inputTokens': 7, 'outputTokens': 1,
                                       'cacheReadTokens': 0, 'cacheWriteTokens': 0}}}),
        # 数字是字符串：认
        json.dumps({'type': 'assistant/message', 'time': '1792248100004',
                    'data': {'usage': {'inputTokens': '8', 'outputTokens': '2',
                                       'cacheReadTokens': '0', 'cacheWriteTokens': '0'},
                             'message': {'source': {'provider': 'p2', 'model': 'm2'}}}}),
        # 坏 JSON：跳过（不计脏，因为是整行坏）
        '{not json',
    ]
    recs, bad = u.parse_file('\n'.join(lines))
    # 记录元组布局：(ts, provider, model, fresh, output, cached, cwrite)
    # 注意下标 1 是 **时间戳**，provider 在 0 位之后 —— 别按「第 1 个是供应商」去写断言。
    check('正常 + 缺字段 + 无 source + 字符串数字 都保留', len(recs) == 4, 'recs=%d' % len(recs))
    check('脏记录被计数', bad == 2, 'bad=%d' % bad)
    check('缺 cacheWriteTokens 按 0', any(r[6] == 0 and r[3] == 1 for r in recs))
    check('无 source 兜底成「未知供应商」', any(r[1] == '未知供应商' for r in recs))
    check('字符串数字被转换', any(r[1] == 'p2' and r[3] == 8 for r in recs))
    # 毫秒时间戳没被当成秒
    check('毫秒时间戳未再被除', all(r[0] > 1e9 for r in recs),
          str([r[0] for r in recs]))


# ------------------------------------------------------------------ 文件发现
def test_find_logs():
    section('会话日志发现（同目录只取最高版本）')
    files = u.find_session_logs()
    check('找到了会话日志', len(files) > 0, '%d 个' % len(files))
    check('每个都是真实文件', all(os.path.isfile(f) for f in files))
    check('没有重复路径', len(files) == len(set(files)))

    # 按会话目录分组，确认每个目录只有一份
    bydir = {}
    for f in files:
        bydir.setdefault(os.path.dirname(f), []).append(f)
    multi = {k: v for k, v in bydir.items() if len(v) > 1}
    check('同目录不出现多份', not multi, str(multi)[:200])

    # 版本都是该目录最高的
    import re
    ok = True
    for d, fs in bydir.items():
        want = max(int(re.match(r'^session\.v(\d+)\.jsonl\.zstd$', os.path.basename(x)).group(1))
                   for x in glob.glob(os.path.join(d, 'session.v*.jsonl.zstd')))
        got = int(re.match(r'^session\.v(\d+)\.jsonl\.zstd$', os.path.basename(fs[0])).group(1))
        if got != want:
            ok = False
    check('取的都是该目录最高版本', ok)


# ------------------------------------------------------------------ 聚合
def test_agg():
    section('聚合口径')
    # 构造两条记录： (sid, ts, provider, model, inp, out, cached, cwrite)
    import datetime
    ts = datetime.datetime(2026, 5, 1, 10, 30).timestamp()
    recs = [
        ('s1', ts, 'p', 'm', 100, 10, 900, 5),      # total = 100+900+5+10 = 1015
        ('s1', ts, 'p', 'm', 0, 0, 0, 0),           # total = 0（仍算一次请求）
    ]
    d = u.agg(recs)
    day = d['days'][0]
    check('fresh 就是 inputTokens', day['fresh'] == 100, str(day))
    check('inputTotal = fresh+cached+cwrite', day['inputTotal'] == 1005, str(day))
    check('total = inputTotal + output', day['total'] == 1015, str(day))
    check('请求数按消息条数', day['req'] == 2, str(day))
    check('模型维度带 provider', d['models'][0]['provider'] == 'p')
    check('recordCount 正确', d['recordCount'] == 2)
    # 天 = 月 = 总量
    check('月度合计等于天合计', d['months'][0]['total'] == day['total'])
    check('小时维度存在', len(d['hours']) == 1 and d['hours'][0]['hour'] == '10')


# ------------------------------------------------------------------ zstd
def test_zstd(deep=False):
    section('zstd 桥')
    check('至少有一个可用后端', zstd_bridge.available(), zstd_bridge.describe())
    print('       当前首选：%s' % zstd_bridge.describe())

    files = u.find_session_logs()
    if not files:
        check('有文件可测', False, '没有会话日志，跳过')
        return
    sample = sorted(files, key=lambda p: os.path.getsize(p))[:3]
    raw = {p: open(p, 'rb').read() for p in sample}
    try:
        out = zstd_bridge.decode_many(raw)
        check('批量解压成功', len(out) == len(sample))
        check('解压结果非空', all(len(v) > 0 for v in out.values()))
        check('解压结果是文本（含 {）', all(b'{' in v for v in out.values()))
    except zstd_bridge.ZstdUnavailable as e:
        check('批量解压成功', False, str(e)[:200])

    if not deep:
        print('       （加 --zstd 可交叉验证四个后端的一致性，较慢）')
        return

    section('zstd 四个后端交叉验证')
    import hashlib
    ref = None
    ref_name = None
    for name in ('stdlib', 'zstandard', 'libzstd', 'node'):
        old = os.environ.get('DSH_TOKEN_ZSTD')
        try:
            os.environ['DSH_TOKEN_ZSTD'] = name
            zstd_bridge._BACKENDS = None          # 强制重新探测
            if not zstd_bridge.available():
                print('  skip %-10s（本机没有这个后端）' % name)
                continue
            got = zstd_bridge.decode_many(raw)
            h = hashlib.sha256(b''.join(got[p] for p in sorted(sample))).hexdigest()
            if ref is None:
                ref, ref_name = h, name
                print('  base %-10s %s' % (name, h[:16]))
            else:
                check('%s 与 %s 一致' % (name, ref_name), h == ref, h[:16])
        finally:
            zstd_bridge._BACKENDS = None
            if old is None:
                os.environ.pop('DSH_TOKEN_ZSTD', None)
            else:
                os.environ['DSH_TOKEN_ZSTD'] = old
    zstd_bridge._BACKENDS = None


# ------------------------------------------------------------------ 真实数据
def test_real_data():
    section('真实数据端到端')
    u.build(force_full=True)
    st = u._STATE
    check('扫描无错误', st['error'] is None, str(st['error']))
    if st['files'] == 0:
        print('  （本机没有会话数据，跳过端到端）')
        return
    check('扫到了文件', st['files'] > 0, '%d' % st['files'])
    check('有请求记录', st['records'] > 0, '%d' % st['records'])
    data = st['data']
    check('days 非空', len(data['days']) > 0)
    check('models 非空', len(data['models']) > 0)
    # 交叉核对：models 汇总 == days 汇总 == months 汇总
    sd = sum(x['total'] for x in data['days'])
    sm = sum(x['total'] for x in data['models'])
    smm = sum(x['total'] for x in data['months'])
    check('天/模型/月 三个维度总量一致', sd == sm == smm,
          'day=%d model=%d month=%d' % (sd, sm, smm))
    # 四个桶加起来 == total
    bad = [x for x in data['days']
           if x['fresh'] + x['cached'] + x['cwrite'] + x['output'] != x['total']]
    check('四个桶之和恒等于 total', not bad, str(bad[:2]))
    # 会话标题大部分应能读到
    titled = [s for s in data['sessions'] if s['title']]
    check('至少一半会话读到标题', len(titled) >= len(data['sessions']) / 2,
          '%d/%d' % (len(titled), len(data['sessions'])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--zstd', action='store_true', help='交叉验证 zstd 四个后端（慢）')
    args = ap.parse_args()

    print('DSH 词元看板 自检')
    print('  程序目录 : %s' % u.APPDIR)
    print('  数据根   : %s（%s）' % (u.DSH, u.DSH_SOURCE))
    print('  zstd     : %s' % zstd_bridge.describe())

    test_data_root()
    test_parse()
    test_find_logs()
    test_agg()
    test_zstd(args.zstd)
    test_real_data()

    print()
    print('=' * 50)
    print('OK=%d  FAIL=%d' % (OK, FAIL))
    print('=' * 50)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
