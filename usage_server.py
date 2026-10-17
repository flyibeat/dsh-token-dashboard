# -*- coding: utf-8 -*-
"""DeepSeek Harness 词元用量看板 —— 本机常驻服务

数据源（全部在本机 DSH 数据根下，只读）：

    <数据根>/sessions/<工作目录转义名>/<会话ID>/session.v<N>.jsonl.zstd
        每个会话一份**事件流**，zstd 压缩（一个文件里拼了成百上千个 zstd 帧）。
        用量只取 `assistant/message` 事件里的 `data.usage`：
            inputTokens / outputTokens / cacheReadTokens / cacheWriteTokens
        模型与供应商取 `data.message.source` 的 `provider` / `model`；
        时间取事件的 `time`（epoch 毫秒）。

    <数据根>/storages/session_projcache/sessions/<会话ID>.json
        会话投影缓存，取**标题**（rows.title.val）与**工作目录**（identity.cwd）。
        只有标题和目录，不含任何正文。

    <数据根>/dsh-usage/usage-ledger.json
        可选。第三方插件 `@linxin666/dsh-usage` 的账本，用作**对账**与**补缺**，
        不参与主统计（详见 README 的「为什么以转录为准」）。

启动：
    python usage_server.py
    浏览器打开 http://127.0.0.1:8793

命令行：
    --port 8800   临时换端口（等价于环境变量 DSH_USAGE_PORT）
    --home <路径> 指定数据根（等价于环境变量 DSH_USAGE_HOME）
    --rebuild     忽略扫描缓存，全量重扫一次
    --open        启动后自动打开浏览器
    --version     只打印本程序版本号后退出

带值的参数两种写法都认：`--port 8800` 与 `--port=8800` 等价（见 _normalize_argv）。

特性：
    * 启动全量扫描一次，之后每 REFRESH_SEC 秒做增量扫描（按 mtime+size 跳过未变文件）
    * 扫描缓存落盘，重启秒级恢复
    * 纯标准库；zstd 解压走 zstd_bridge 的四级降级
    * 不联网，默认只监听 127.0.0.1
"""
import os
import sys
import json
import glob
import time
import threading
import datetime
import collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import zstd_bridge


# ---- 控制台编码兜底 ---------------------------------------------------------
#
# 本程序所有提示都是中文。Windows 英文版控制台的代码页是 cp1252，直接 print 中文会
# UnicodeEncodeError 崩掉。统一切到 UTF-8；实在编不出来就退化为替换字符 ——
# 绝不能因为「打印一行日志」而退出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


# ---- 命令行规范化 -----------------------------------------------------------
#
# `--port 8800` 与 `--port=8800` 都要能用。`'--port' in sys.argv` 是精确匹配，
# `--port=8800` 整个是一个元素，会**静默落空**（不报错、悄悄用默认端口），
# 比报错更难查。这里统一把 `--key=value` 拆成 `--key value`，所有地方只读 ARGV。
def _normalize_argv(argv):
    out = []
    for a in argv:
        if a.startswith('--') and '=' in a:
            k, v = a.split('=', 1)
            out += [k, v]
        else:
            out.append(a)
    return out


ARGV = _normalize_argv(sys.argv)

# ---- 版本 -------------------------------------------------------------------
#
# 前后端各自计版本，互不牵连：后端 = 本常量，前端 = dashboard.html 里的 FE_VER。
# 页面页头会同时显示两者，一眼看出当前跑的是哪个组合。
BE_VER = os.environ.get('DSH_VERSION', '').strip() or 'v1.0.0'


# ---- 数据根定位 -------------------------------------------------------------
#
# 数据固定放在「数据根」下，即用户目录里的 .dsh（`$DSH_HOME`）。
#
# 为什么不直接写 os.path.expanduser('~')：
#   Windows 上 expanduser 只认 USERPROFILE。注册成服务（LocalSystem）或计划任务跑时，
#   该变量指向服务账户自己的 profile，于是程序去空目录里找数据 —— 服务在跑、
#   端口在听、日志在写，可看板上每个数字都是 0，且不报任何错，极易误判成
#   「数据没同步」。所以四级定位，越靠前越优先：
#
#     1. DSH_USAGE_HOME 环境变量  —— 显式指定，写错也尊重（会告警）
#     2. DSH_HOME 环境变量        —— DSH 自己就在用这个，最贴合
#     3. ~/.dsh 存在              —— 普通场景走这条
#     4. 自动探测各用户目录       —— 扫 <盘>:\Users\*\.dsh（POSIX 为 /Users、/home），
#                                    唯一命中即采用；多个命中按用户名、再按会话数择优
#
# 第 4 级跨平台，所以它不只服务这一台机器 —— 换机、换系统都不必再想起这件事。

def _normpath(p):
    return os.path.normcase(os.path.normpath(p))


def _looks_like_dsh(d):
    """目录是否像 DSH 的数据根（有 sessions/ 或 storages/ 或 dsh-usage/）。"""
    if not d or not os.path.isdir(d):
        return False
    return any(os.path.isdir(os.path.join(d, sub))
               for sub in ('sessions', 'storages', 'dsh-usage'))


def _count_sessions(d, cap=500):
    """会话文件数，只用作多候选时的择优依据。"""
    n = 0
    try:
        for root, dirs, files in os.walk(os.path.join(d, 'sessions')):
            for f in files:
                if f.startswith('session.') and f.endswith('.jsonl.zstd'):
                    n += 1
                    if n >= cap:
                        return cap
    except Exception:
        pass
    return n


def _dsh_score(d):
    """可信度：先看是否与当前用户同名（服务场景下 USERNAME 仍会带上），再看数据量。"""
    owner = os.path.basename(os.path.dirname(d)).lower()
    me = (os.environ.get('USERNAME') or os.environ.get('USER') or '').lower()
    return (1 if (me and owner == me) else 0, _count_sessions(d))


def _profile_containers():
    """可能存放 <用户名> 目录的容器。"""
    if os.name == 'nt':
        out, seen = [], set()
        drives = [os.environ.get('SystemDrive', 'C:')]
        drives += [c + ':' for c in 'DEFGHIJKLMNOPQRSTUVWXYZ']
        for drv in drives:
            p = os.path.join(drv + os.sep, 'Users')
            k = _normpath(p)
            if k not in seen and os.path.isdir(p):
                seen.add(k)
                out.append(p)
        return out
    return [p for p in ('/Users', '/home') if os.path.isdir(p)]


def _resolve_data_root():
    """返回 (数据根, 来源说明, 探测过的候选列表)。"""
    # 1) 显式指定：DSH_USAGE_HOME（或命令行 --home 灌进来的同名变量）
    env = (os.environ.get('DSH_USAGE_HOME') or '').strip().strip('"').strip("'")
    if env:
        env = os.path.expandvars(env)
        got = os.path.abspath(env)
        if _looks_like_dsh(got):
            return got, 'DSH_USAGE_HOME', []
        for sub in ('.dsh', 'dsh'):
            cand = os.path.join(got, sub)
            if _looks_like_dsh(cand):
                return cand, 'DSH_USAGE_HOME（自动补 %s）' % sub, []
        return got, 'DSH_USAGE_HOME（该路径不存在）', []

    # 2) DSH 自己的数据根环境变量
    env = (os.environ.get('DSH_HOME') or '').strip().strip('"').strip("'")
    if env:
        got = os.path.abspath(os.path.expandvars(env))
        if _looks_like_dsh(got):
            return got, 'DSH_HOME', []

    # 3) 常规展开 —— 非服务场景全部走这条
    try:
        _home = os.path.expanduser('~')
    except Exception:
        _home = ''
    if not _home or _home == '~':
        _home = os.environ.get('USERPROFILE') or os.environ.get('HOME') or ''
    default = os.path.join(_home, '.dsh') if _home else os.path.join('.', '.dsh')
    if _looks_like_dsh(default):
        return default, '用户目录', []

    # 4) 自动探测
    cands = [default]
    for cont in _profile_containers():
        try:
            for name in sorted(os.listdir(cont)):
                cands.append(os.path.join(cont, name, '.dsh'))
        except Exception:
            continue
    for k in ('USERPROFILE', 'HOME'):
        v = os.environ.get(k)
        if v:
            cands.append(os.path.join(v, '.dsh'))
    uniq, seen = [], set()
    for c in cands:
        k = _normpath(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    hits = [c for c in uniq if _looks_like_dsh(c)]
    if len(hits) == 1:
        return hits[0], '自动探测（唯一命中）', uniq
    if len(hits) > 1:
        hits.sort(key=_dsh_score, reverse=True)
        return hits[0], '自动探测（%d 个候选，按用户名/会话数择优）' % len(hits), uniq

    # 5) 回退：行为等同默认，由自检块负责解释
    return default, '回退默认（未找到任何数据根）', uniq


# --home 必须在解析之前就灌进环境变量，才不影响模块级求值顺序
if '--home' in ARGV:
    _i = ARGV.index('--home')
    if _i + 1 < len(ARGV):
        os.environ['DSH_USAGE_HOME'] = ARGV[_i + 1]

DSH, DSH_SOURCE, DSH_CANDIDATES = _resolve_data_root()
SESSIONS = os.path.join(DSH, 'sessions')
PROJCACHE = os.path.join(DSH, 'storages', 'session_projcache', 'sessions')
LEDGER_DIR = os.path.join(DSH, 'dsh-usage')
LEDGER = os.path.join(LEDGER_DIR, 'usage-ledger.json')


def _exe_dir():
    """程序目录。打包成 exe 时是 exe 所在目录，否则是本脚本目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _res_dir():
    """内嵌资源的解压目录。PyInstaller onefile 解到 sys._MEIPASS —— 那是**临时**
    目录、进程退出即删，所以只能放随包资源，绝不能放缓存和日志。"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', _exe_dir())
    return os.path.dirname(os.path.abspath(__file__))


APPDIR = _exe_dir()          # 数据落这里：缓存、日志、可选的外部前端
RESDIR = _res_dir()          # 随包资源读这里：内嵌的 dashboard.html 兜底副本
CACHE_PATH = os.path.join(APPDIR, 'scan-cache.json')
LOG_PATH = os.path.join(APPDIR, 'server.log')

# scan-cache.json 的结构版本：**解析口径一改就要 +1**。
# load_cache() 只认当前版本，读到旧版本直接丢弃 → 下一次 build() 全量重解析。
# 没有这道闸门时，改了取值口径、重启后数字一动不动 —— 因为缓存里存的是
# **上次解析出来的结果**，文件签名没变就不会重解析。
#   v1：初版。会话按「目录取最高版本」+ 取 assistant/message 的 usage。
CACHE_VER = 1

PORT = int(os.environ.get('DSH_USAGE_PORT', '8793'))
BIND = os.environ.get('DSH_USAGE_BIND', '127.0.0.1')
REFRESH_SEC = int(os.environ.get('DSH_USAGE_REFRESH', '60'))
# 会话标题/目录（来自 projcache）的缓存时长，秒。标题很少变，不必每次刷新都读。
SESSION_META_TTL = int(os.environ.get('DSH_USAGE_META_TTL', '300'))

_LOCK = threading.Lock()
# build() 的单飞锁：force=1 与 refresher 并发时后到者直接跳过
_BUILD_LOCK = threading.Lock()
_meta_cache = {'at': 0.0, 'map': {}}
# 会话标题读 cache 的健康状态。存在的意义：读失败只在 server.log 里写一行
# 「不影响统计」，页面上完全看不出来 —— 会话列会悄悄退化成显示会话 ID，
# 属于最危险的那种「静默降级」。现在把它暴露到 /api/health，让降级可见。
_meta_health = {'ok': None, 'error': None, 'at': 0.0}
_STATE = {
    'data': None,
    'built_at': None,
    'scan_seconds': None,
    'files': 0,
    'records': 0,
    'skipped': 0,          # 字段类型异常被跳过的记录数（>0 说明统计可能偏低）
    'error': None,
    'building': False,
}


def _console_supports_color():
    """能不能在控制台上用 ANSI 颜色。作为服务/计划任务运行时 stdout 不是 tty，
    往日志里塞转义序列只会变成乱码，所以逐层判断。"""
    if os.environ.get('NO_COLOR'):
        return False
    try:
        if not (sys.stdout and sys.stdout.isatty()):
            return False
    except Exception:
        return False
    if os.name != 'nt':
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def log(msg, level=''):
    """写一行日志。level='warn' 时控制台标红；日志文件里始终是纯文本。

    注意：走 stderr 而不是 stdout。本模块会被别的程序 import 来当「数据源」，
    日志若混在 stdout 里会污染 JSON。
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    plain = '[%s] %s' % (ts, msg)
    out = plain
    if level == 'warn' and _console_supports_color():
        out = '\033[31m%s\033[0m' % plain
    try:
        print(out, file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as fh:
            fh.write(plain + '\n')
    except Exception:
        pass


def _is_loopback(bind):
    """判断绑定地址是否只对本机可见。空串会绑到所有网卡（等于 0.0.0.0），也算非回环。"""
    b = (bind or '').strip().lower()
    if b in ('localhost', '::1', '[::1]'):
        return True
    return b.startswith('127.')


def warn_exposed(bind, port):
    """非回环绑定时的暴露告警。

    默认 127.0.0.1 时本机之外谁也读不到；一旦绑成 0.0.0.0（或某个具体网卡地址），
    同网段任何人都能访问 /api/data 读到会话标题、工作目录与用量数字 —— 本服务
    **没有内置鉴权**。改配置的人不一定读 README，所以启动日志里再拦一道，
    并且把真实绑定地址打出来。
    """
    for line in (
        '=' * 62,
        '警告：本服务正在对外暴露（监听 %s:%d）' % (bind or '0.0.0.0', port),
        '  本服务没有内置鉴权：能连到这个端口的人，都能读到你的会话标题、',
        '  工作目录与用量数字（会话正文不读、也不下发）。',
        '  只想本机使用：把 DSH_USAGE_BIND 设回 127.0.0.1（默认值）；确实要对外，',
        '  请在前面套一层带鉴权的反向代理，或自行限制防火墙来源。',
        '=' * 62,
    ):
        log(line, level='warn')


_EMPTY_WARNED = {'done': False}


def _warn_empty_scan():
    """扫到 0 个会话文件时说清原因 —— 这种故障的全部特征就是「一切正常但数字是 0」，
    光看服务状态和端口永远查不出来，所以直接把定位过程打出来。每个进程只报一次。"""
    if _EMPTY_WARNED['done']:
        return
    _EMPTY_WARNED['done'] = True
    log('-' * 62)
    log('未扫到任何会话文件，看板会是空的。定位信息：')
    log('  选中的数据根 : %s' % DSH)
    log('  判定来源     : %s' % DSH_SOURCE)
    log('  会话目录     : %s' % SESSIONS)
    log('  程序目录     : %s' % APPDIR)
    for k in ('DSH_HOME', 'DSH_USAGE_HOME', 'USERPROFILE', 'USERNAME', 'HOME', 'USER'):
        v = os.environ.get(k)
        if v:
            log('  环境变量 %-14s= %s' % (k, v))
    if DSH_CANDIDATES:
        log('  探测过的候选：')
        for c in DSH_CANDIDATES[:15]:
            log('    [%s] %s' % ('有数据' if _looks_like_dsh(c) else '不存在 ', c))
        if len(DSH_CANDIDATES) > 15:
            log('    …另有 %d 个' % (len(DSH_CANDIDATES) - 15))
    ex = r'C:\Users\<用户名>\.dsh' if os.name == 'nt' else '/Users/<用户名>/.dsh'
    log('  修法：把数据根指对，二者等价 ——')
    log('    环境变量  DSH_USAGE_HOME=%s' % ex)
    log('    命令行    --home "%s"' % ex)
    log('-' * 62)


# ---------------------------------------------------------------- 扫描与解析

def _num(v, cast):
    """把一条记录里的字段值转成数值；**不可用就返回 None**。

    容错范围故意收窄：
      * None / 空串 → 0（缺字段在历史数据里很常见）
      * int / float → 正常转换（bool 要先挡掉：Python 里 True 也是 int）
      * 数字字符串（"123" / "1.5e3"）→ 认，换版本时字段改成字符串也不至于崩
      * 其余（dict / list / "abc" 之类）→ None

    返回 None 时由调用方**丢掉整条记录并计数上报**。宁可少算并大声报出来，
    也不要静默按 0 混进统计 —— 那比崩掉更难发现。
    """
    if v is None or v == '':
        return cast(0)
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return cast(v)
    if isinstance(v, str):
        try:
            return cast(float(v))
        except Exception:
            return None
    return None


# 会话日志文件名：session.v<N>.jsonl.zstd，N 是格式版本。
import re as _re
_LOG_RE = _re.compile(r'^session\.v(\d+)\.jsonl\.zstd$')


def find_session_logs():
    """找出每个会话**一份**日志。

    目录结构是 `<sessions>/<工作目录转义名>/<会话ID>/session.v<N>.jsonl.zstd`
    —— 注意是**两层**（工作区 → 会话），不是一层。用 os.walk 递归找，
    这样多一层少一层都不会漏。

    同一会话目录里可能同时留着 v3 和 v4：它们是同一个会话在不同格式版本下的
    副本，只能取版本最高的那份，否则用量会被重复累加（本机实测 39 个会话目录
    里有 1 个同时有 v3+v4）。
    """
    best = {}
    try:
        walker = os.walk(SESSIONS)
    except Exception:
        return []
    for root, _dirs, names in walker:
        ver = -1
        pick = None
        for name in names:
            m = _LOG_RE.match(name)
            if not m:
                continue
            v = int(m.group(1))
            if v > ver:
                ver = v
                pick = os.path.join(root, name)
        if pick:
            best[root] = (ver, pick)
    # 只返回路径（版本号只用于「同目录取最高版本」这一步的取舍）
    return [best[k][1] for k in sorted(best)]


def parse_file(text):
    """解析单个会话日志（已解压的文本），返回 (记录列表, 被跳过的脏记录数)。

    跳过策略统一为「一坏只跳这一条，其余照常」。每个取值都可能是脏数据 ——
    timestamp 比较、model 当字典键这两处同样会炸，所以一并放进同一条保护里。

    为什么值得这层保护：异常会一路冒到 build() 的 except，那里只置 _STATE['error']、
    保留上一次的数据，于是看板**永久停在旧数据**上；更麻烦的是 save_cache() 排在
    scan() 之后根本走不到，缓存永不再落盘，每分钟全量重解析一遍。
    """
    recs = []
    bad = 0
    for line in text.splitlines():
        # 快筛：绝大多数事件（tool/result 之类）没有 usage，先按字面量掉。
        # **只筛 "usage"，不筛 "source"** —— source 是可选字段，缺失时要用
        # 「未知供应商/模型」兜底，筛掉它会把这类记录整条漏掉（自检里就踩到过）。
        if '"usage"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get('type') != 'assistant/message':
            continue
        d = o.get('data')
        if not isinstance(d, dict):
            continue
        u = d.get('usage')
        if not isinstance(u, dict):
            continue
        src = ((d.get('message') or {}).get('source')
               if isinstance(d.get('message'), dict) else None)
        if not isinstance(src, dict):
            src = {}
        provider = src.get('provider')
        provider = provider.strip() if isinstance(provider, str) else ''
        model = src.get('model')
        model = model.strip() if isinstance(model, str) else ''
        ts = _num(o.get('time'), float)
        if ts is None:
            bad += 1
            continue
        if not ts:
            continue                        # 没有时间戳 ≠ 脏数据，只是不参与统计
        # 秒 / 毫秒自适应。阈值取 1e11 而不是 1e12：
        #   秒级时间戳现在约 1.8e9，要到公元 5138 年才够 1e11；
        #   毫秒级现在是 1.8e12，远大于 1e11。
        # 用 1e12 当阈值会把**当前**的毫秒值误判成秒 —— 测试里就踩到了：
        # 1792248100004 被除以 1000 后落到 1970 年，记录看着「解析成功」其实日期全错。
        if ts > 1e11:
            ts = ts / 1000.0
        inp = _num(u.get('inputTokens'), int)
        out = _num(u.get('outputTokens'), int)
        cached = _num(u.get('cacheReadTokens'), int)
        cwrite = _num(u.get('cacheWriteTokens'), int)
        if inp is None or out is None or cached is None or cwrite is None:
            bad += 1
            continue
        recs.append([ts, provider or '未知供应商', model or '未知模型',
                     inp, out, cached, cwrite])
    return recs, bad


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, 'r', encoding='utf-8') as fh:
            obj = json.load(fh)
        if isinstance(obj, dict) and obj.get('v') == CACHE_VER:
            return {k: v for k, v in (obj.get('files') or {}).items()}
    except Exception:
        pass
    return {}


def save_cache(cache):
    tmp = CACHE_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'v': CACHE_VER, 'files': cache}, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        log('缓存写入失败: %s' % e)


def scan(cache):
    """增量扫描，返回 (新缓存, 记录, 重解析的文件数, 被跳过的脏记录数)。

    脏记录数**按文件存进缓存**再汇总，而不是只统计本轮解析出来的那些 ——
    否则重启后走了缓存，这个数会变回 0，明明还有脏记录却不再告警。
    """
    files = find_session_logs()
    new_cache = {}
    records = []
    parsed = 0
    bad = 0
    # 先把「需要重解析」的文件挑出来，再一次性交给 zstd_bridge 批量解压 ——
    # Node 那一级要起进程，逐文件调用会把启动开销乘以文件数。
    need = []
    for fp in files:
        try:
            st = os.stat(fp)
        except Exception:
            continue
        sig = [int(st.st_mtime), int(st.st_size)]
        ent = cache.get(fp)
        if ent and list(ent.get('sig') or []) == sig:
            # 签名没变：直接复用上次解析结果
            new_cache[fp] = {'sig': sig, 'recs': ent.get('recs') or [],
                             'bad': int(ent.get('bad') or 0)}
        else:
            # 签名变了或没缓存：先占位，等批量解压后填
            new_cache[fp] = {'sig': sig, 'recs': None, 'bad': 0}
            need.append(fp)

    raw_map = {}
    for fp in need:
        try:
            with open(fp, 'rb') as fh:
                raw_map[fp] = fh.read()
        except Exception as e:
            log('读取失败（跳过该会话）：%s（%r）' % (fp, e))
            new_cache.pop(fp, None)

    if raw_map:
        try:
            decoded = zstd_bridge.decode_many(raw_map)
        except zstd_bridge.ZstdUnavailable as e:
            # 解压全挂：**不能**把缓存写成空，否则下次还得重来。
            # 直接抛出，让 build() 置 error、保留上一轮的数据。
            raise RuntimeError('zstd 解压不可用，无法读取会话日志：%s' % e)
        for fp, blobs in decoded.items():
            text = blobs.decode('utf-8', 'replace')
            recs, nbad = parse_file(text)
            ent = new_cache.get(fp)
            if ent is not None:
                ent['recs'] = recs
                ent['bad'] = nbad
            parsed += 1

    for fp, ent in new_cache.items():
        if ent.get('recs') is None:      # 读失败没补上：这轮先不算它
            ent['recs'] = []
        bad += int(ent.get('bad') or 0)
        sid = os.path.basename(os.path.dirname(fp))
        for r in ent['recs']:
            records.append((sid,) + tuple(r))
    return new_cache, records, parsed, bad


# ---------------------------------------------------------------- 聚合

def agg(records):
    """把逐条记录聚成看板要的各个维度。

    词元口径（四个桶互不重叠，加起来就是计费输入 + 输出）：

        fresh   = 未命中缓存的输入词元（真正按新输入计价的那部分）
        cached  = 命中提示缓存的输入词元（便宜）
        cwrite  = 写入缓存的输入词元（部分供应商按略高于普通输入计价）
        output  = 输出词元（reasoning 已含在里面）
        total   = fresh + cached + cwrite + output
        inputTotal = fresh + cached + cwrite（= 计费输入合计，做缓存命中率的分母）

    注意 DSH 的 `usage.inputTokens` **本身就是未命中缓存的输入**，不像有些平台
    把「输入合计」放在这个字段里、再用一个命中数去减。所以这里 fresh 直接等于
    inputTokens，**没有**「input − cached」这种减法。
    """
    day = collections.defaultdict(collections.Counter)
    day_model = collections.defaultdict(collections.Counter)
    day_hour = collections.defaultdict(collections.Counter)
    ses = collections.defaultdict(collections.Counter)
    month = collections.defaultdict(collections.Counter)
    n = 0
    tmin = tmax = None

    def add(c, inp, out, cached, cwrite):
        c['req'] += 1
        c['fresh'] += inp
        c['cached'] += cached
        c['cwrite'] += cwrite
        c['output'] += out
        c['inputTotal'] += inp + cached + cwrite
        c['total'] += inp + cached + cwrite + out

    for sid, ts, provider, model, inp, out, cached, cwrite in records:
        n += 1
        dt = datetime.datetime.fromtimestamp(ts)
        d = dt.strftime('%Y-%m-%d')
        add(day[d], inp, out, cached, cwrite)
        add(day_model[(d, provider, model)], inp, out, cached, cwrite)
        add(day_hour[(d, dt.strftime('%H'))], inp, out, cached, cwrite)
        add(ses[sid], inp, out, cached, cwrite)
        add(month[d[:7]], inp, out, cached, cwrite)
        tmin = ts if tmin is None or ts < tmin else tmin
        tmax = ts if tmax is None or ts > tmax else tmax

    days = [{'date': k, **dict(day[k])} for k in sorted(day)]
    models = [{'date': k[0], 'provider': k[1], 'model': k[2], **dict(v)}
              for k, v in sorted(day_model.items())]

    # 会话维度：带上标题与工作目录，免得前端只能显示看不懂的会话 ID
    meta = load_session_meta()
    sessions = []
    for k, v in ses.items():
        m = meta.get(k) or {}
        item = {
            'session': k,
            'title': m.get('title') or '',
            'cwd': m.get('cwd') or '',
        }
        item.update(dict(v))
        sessions.append(item)
    sessions = sorted(sessions, key=lambda x: -x['total'])[:60]

    months = [{'month': k, **dict(v)} for k, v in sorted(month.items())]
    hours = [{'date': k[0], 'hour': k[1], **dict(v)} for k, v in sorted(day_hour.items())]

    return {
        'generatedAt': datetime.datetime.now().isoformat(timespec='seconds'),
        'source': SESSIONS,
        'span': {
            'from': datetime.datetime.fromtimestamp(tmin).isoformat(timespec='seconds') if tmin else None,
            'to': datetime.datetime.fromtimestamp(tmax).isoformat(timespec='seconds') if tmax else None,
        },
        'days': days,
        'months': months,
        'models': models,
        'sessions': sessions,
        'hours': hours,
        'recordCount': n,
        'summary': current_usage_summary(ses),
    }


def load_session_meta(force=False):
    """读 projcache 的 <会话ID>.json，取会话标题与工作目录。

    用途：看板上「单会话消耗」原先只显示会话 ID，人看不懂 ID 对应哪次对话，
    所以把标题一起下发。

    缓存 SESSION_META_TTL 秒；读失败不影响主流程 —— 返回空表，
    前端会退回显示会话 ID，统计数字不受任何影响。
    但**失败会被记进 `_meta_health` 并下发到 `/api/health`**，不再只写日志。
    """
    now = time.time()
    if not force and _meta_cache['map'] and now - _meta_cache['at'] < SESSION_META_TTL:
        return _meta_cache['map']
    if not os.path.isdir(PROJCACHE):
        _meta_health.update(ok=False, error='投影缓存目录不存在：%s' % PROJCACHE, at=now)
        return _meta_cache['map'] or {}
    out = {}
    err = None
    try:
        for name in os.listdir(PROJCACHE):
            if not name.endswith('.json'):
                continue
            p = os.path.join(PROJCACHE, name)
            try:
                with open(p, 'r', encoding='utf-8') as fh:
                    o = json.load(fh)
            except Exception:
                continue
            rec = ((o.get('record') or {}) if isinstance(o, dict) else {})
            rows = rec.get('rows') or {}
            title = ''
            t = rows.get('title')
            if isinstance(t, dict) and isinstance(t.get('val'), str):
                title = t['val'].strip()
            ident = rec.get('identity') or {}
            cwd = ident.get('cwd') if isinstance(ident.get('cwd'), str) else ''
            out[name[:-5]] = {'title': title, 'cwd': cwd or ''}
    except Exception as e:
        err = repr(e)
    if err:
        log('读取会话标题失败（不影响统计）：%s' % err)
        _meta_health.update(ok=False, error=err, at=now)
        return _meta_cache['map'] or {}
    _meta_cache['at'] = now
    _meta_cache['map'] = out
    _meta_health.update(ok=True, error=None, at=now)
    return out


def load_ledger():
    """读第三方插件 `@linxin666/dsh-usage` 的账本，**只用于对账**。

    返回 (账本或 None, 说明)。账本形状：
        {"version":1,"days":{"YYYY-MM-DD":{"<provider>":{"<model>":{...}}}}}
    """
    if not os.path.exists(LEDGER):
        return None, '未安装 dsh-usage 插件，无账本可比（不影响统计）'
    try:
        with open(LEDGER, 'r', encoding='utf-8') as fh:
            o = json.load(fh)
        if not isinstance(o, dict) or not isinstance(o.get('days'), dict):
            return None, '账本结构不认识'
        return o, None
    except Exception as e:
        return None, '账本读取失败：%r' % e


def _ledger_totals(doc):
    """把账本折成 {(日期, provider, model): 总词元} 与总量。"""
    per = {}
    if not doc:
        return per
    for day, provs in (doc.get('days') or {}).items():
        if not isinstance(provs, dict):
            continue
        for prov, models in provs.items():
            if not isinstance(models, dict):
                continue
            for model, t in models.items():
                if not isinstance(t, dict):
                    continue
                try:
                    tot = (float(t.get('inputTokens') or 0) + float(t.get('cacheReadTokens') or 0)
                           + float(t.get('cacheWriteTokens') or 0) + float(t.get('outputTokens') or 0))
                except Exception:
                    continue
                per[(day, prov, model)] = per.get((day, prov, model), 0.0) + tot
    return per


def current_usage_summary(ses):
    """给 `/api/data` 附一个**只关于本看板自己**的汇总。

    这里原本是和 dsh-usage 账本的对账块。那一块已经从页面上撤掉了（用户要求），
    所以接口里不再附带账本读数 —— 每轮刷新少读一个文件，也不会再有
    「页面上的数字和账本对不上」这类噪音。

    真要对账请用 `reconcile.py`：它是独立命令行工具，把差异定位到哪一天 / 哪个
    供应商 / 哪个模型，并用 DSH 自己的 tokenUsage 投影缓存做权威对照。
    """
    return {
        'sessions': len(ses),
    }


# ---------------------------------------------------------------- 刷新线程

def build(force_full=False):
    """重扫 + 聚合 + 更新 _STATE。返回是否真的跑了一轮（被单飞挡掉时返回 False）。

    单飞：同一时刻只允许一个 build 在跑，force=1 与 refresher 撞上时后到者直接跳过。
    并发跑没有任何好处（两个线程在做同一件全量重扫），坏处却很实在：
      * 各自 load_cache() → scan() → save_cache()，后写的缓存可能基于更早的一次读取，
        把新解析的结果覆盖成**陈旧缓存**，下一轮白解析一遍；
      * 连点「刷新」会不断新起线程，越积越多。
    """
    if not _BUILD_LOCK.acquire(blocking=False):
        log('已有刷新在进行中，本次跳过')
        return False
    started = time.time()
    try:
        with _LOCK:
            _STATE['building'] = True
        try:
            cache = {} if force_full else load_cache()
            new_cache, records, parsed, skipped = scan(cache)
            # 仅在真有变化时落盘：文件集合与重解析数都没变，内容必然一样。
            # 否则每 60 秒无条件重写一份几百 KB 的缓存（也等于让同步盘每分钟上传一次）。
            if parsed or len(new_cache) != len(cache):
                save_cache(new_cache)
            data = agg(records)
            with _LOCK:
                _STATE.update({
                    'data': data,
                    'built_at': time.time(),
                    'scan_seconds': round(time.time() - started, 2),
                    'files': len(new_cache),
                    'records': len(records),
                    'skipped': skipped,
                    'error': None,
                })
            log('刷新完成：%d 文件（重解析 %d）/ %d 条记录 / 耗时 %.1fs'
                % (len(new_cache), parsed, len(records), time.time() - started))
            if skipped:
                log('注意：%d 条请求记录的字段类型异常已跳过，看板总量略低于实际' % skipped)
            if not new_cache:
                _warn_empty_scan()
        except Exception as e:
            with _LOCK:
                _STATE['error'] = repr(e)
            log('刷新失败：%r' % e)
        finally:
            with _LOCK:
                _STATE['building'] = False
        return True
    finally:
        _BUILD_LOCK.release()


def refresher():
    while True:
        try:
            build()
        except Exception as e:
            log('刷新线程异常：%r' % e)
        time.sleep(REFRESH_SEC)


# ---------------------------------------------------------------- 看板前端

def html_path():
    """前端文件位置。**优先用程序目录下的外部 dashboard.html** —— 打包成 exe 后前端
    仍是普通文件，改完存盘 + 刷新浏览器即生效，不必重新打包；没有外部文件才退回
    随包内嵌的那份。"""
    ext = os.path.join(APPDIR, 'dashboard.html')
    if os.path.exists(ext):
        return ext
    emb = os.path.join(RESDIR, 'dashboard.html')
    return emb if os.path.exists(emb) else ext


_html_cache = {'mtime': None, 'body': ''}


def html_mtime():
    """前端文件的修改时间，作为版本号下发给页面，用于自动重载。"""
    try:
        return int(os.stat(html_path()).st_mtime)
    except Exception:
        return 0


def load_html():
    """前端页面独立存放，改动后只要存盘 + 刷新浏览器即生效，不必重启服务。"""
    try:
        st = os.stat(html_path())
    except Exception as e:
        return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                '<title>frontend missing</title></head>'
                '<body style="font-family:sans-serif;padding:40px">'
                '<h2>dashboard.html not found</h2><pre>%s</pre></body></html>' % e)
    if _html_cache['mtime'] != st.st_mtime or not _html_cache['body']:
        with open(html_path(), 'r', encoding='utf-8') as fh:
            _html_cache['body'] = fh.read()
        _html_cache['mtime'] = st.st_mtime
    return _html_cache['body']


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = 'DSHUsage/1.0'

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html', '/dashboard'):
            self._send(200, load_html(), 'text/html; charset=utf-8')
            return
        if path == '/api/data':
            force = 'force=1' in self.path
            if force:
                # 已经在刷新就不再新起线程：build() 自己有单飞锁会跳过，但连点刷新
                # 会白攒一堆线程，这里干脆不创建。
                with _LOCK:
                    already = _STATE['building']
                if not already:
                    threading.Thread(target=build, daemon=True).start()
            with _LOCK:
                data = _STATE['data']
                busy = _STATE['building']
                err = _STATE['error']
                skipped = _STATE['skipped']
            if data is None:
                self._send(200, json.dumps({'generatedAt': '', 'days': [], 'months': [],
                                            'models': [], 'sessions': [], 'hours': [],
                                            'recordCount': 0, 'refreshSec': REFRESH_SEC,
                                            'serverVersion': BE_VER, 'parseSkipped': skipped,
                                            'scanError': err or '首次扫描进行中，请稍后刷新'},
                                           ensure_ascii=False), 'application/json; charset=utf-8')
                return
            out = dict(data)
            out['refreshSec'] = REFRESH_SEC
            out['building'] = busy
            out['scanError'] = err
            out['parseSkipped'] = skipped
            out['htmlMtime'] = html_mtime()
            out['serverVersion'] = BE_VER
            self._send(200, json.dumps(out, ensure_ascii=False), 'application/json; charset=utf-8')
            return
        if path == '/api/health':
            with _LOCK:
                st = {'records': _STATE['records'], 'files': _STATE['files'],
                      'builtAt': _STATE['built_at'], 'error': _STATE['error'],
                      'scanSeconds': _STATE['scan_seconds'], 'building': _STATE['building'],
                      'parseSkipped': _STATE['skipped'],
                      'version': BE_VER,
                      'dataRoot': DSH if _is_loopback(BIND) else os.path.basename(DSH),
                      'dataRootSource': DSH_SOURCE,
                      'zstdBackend': zstd_bridge.describe(),
                      'zstdActive': zstd_bridge.active(),
                      # 会话标题读 cache 的健康状态。None = 还没读过。
                      # 这两个字段让「会话列悄悄退化成显示 ID」这种静默降级可见。
                      'sessionMetaOk': _meta_health['ok'],
                      'sessionMetaError': _meta_health['error']}
                if not _is_loopback(BIND):
                    # 非回环绑定 = 同网段可读这个接口，别把本机绝对路径（含用户名）送出去
                    st['dataRootRedacted'] = True
            self._send(200, json.dumps(st, ensure_ascii=False), 'application/json; charset=utf-8')
            return
        self._send(404, 'not found', 'text/plain; charset=utf-8')

    def log_message(self, fmt, *args):
        pass


def _pause_if_interactive():
    """双击运行出错时留住窗口，免得报错一闪而过；作为计划任务运行时不会阻塞。"""
    try:
        if sys.stdin and sys.stdin.isatty():
            input('按回车键退出...')
    except Exception:
        pass


def main():
    global PORT
    if '--version' in ARGV or '-V' in ARGV:
        print('dsh-usage %s' % BE_VER)
        return
    if '--port' in ARGV:
        try:
            PORT = int(ARGV[ARGV.index('--port') + 1])
        except Exception:
            log('--port 参数无效，沿用 %d' % PORT)
    frozen = getattr(sys, 'frozen', False)
    log('启动中，版本 %s，端口 %s:%d，数据源 %s' % (BE_VER, BIND or '0.0.0.0', PORT, SESSIONS))
    log('数据根判定：%s（来源：%s）' % (DSH, DSH_SOURCE))
    log('zstd 解压后端：%s' % zstd_bridge.describe())
    if frozen:
        log('程序目录 %s ｜ 资源目录 %s' % (APPDIR, RESDIR))
    if not zstd_bridge.available():
        log('严重：没有任何可用的 zstd 解压后端，会话日志读不出来。', level='warn')
        log('  装一个：pip install zstandard；或让 PATH 上有 libzstd / node(≥22.15)。', level='warn')
    if not _is_loopback(BIND):
        warn_exposed(BIND, PORT)
    build(force_full=('--rebuild' in ARGV))
    threading.Thread(target=refresher, daemon=True).start()
    try:
        srv = ThreadingHTTPServer((BIND, PORT), Handler)
    except OSError as e:
        log('端口 %d 绑定失败：%s' % (PORT, e))
        log('可能已有一个实例在跑。换端口：设环境变量 DSH_USAGE_PORT，或用 --port 8800')
        _pause_if_interactive()
        raise SystemExit(1)
    url = 'http://%s:%d' % ('127.0.0.1' if (not BIND or BIND in ('0.0.0.0', '::')) else BIND, PORT)
    log('服务就绪：%s' % url)
    if not _is_loopback(BIND):
        log('实际监听：%s:%d（非回环，同网段可访问 —— 见上方暴露告警）' % (BIND or '0.0.0.0', PORT))
    if frozen:
        print('')
        print('  DeepSeek Harness 词元用量看板已启动')
        print('  版本：%s' % BE_VER)
        print('  浏览器打开：%s' % url)
        print('  数据根：%s' % DSH)
        print('  关闭本窗口即停止服务；缓存与日志都写在同目录下。')
        print('')
        if '--open' in ARGV:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log('收到中断，退出')
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
