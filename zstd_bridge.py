#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zstd 桥接：给没有 zstd 支持的老 Python 解压 DSH 的会话日志。

为什么需要这个文件
==================

DSH 把每个会话的事件流写成 `session.v<N>.jsonl.zstd` —— **zstd 压缩**的 JSONL。
而「零依赖」的纯标准库 Python 只有 3.14 起才有 `compression.zstd`。
DSH 自己托管的 Python 运行时是 **3.12**（`$DSH_HOME/dsh-runtimes/*/dependencies/python`），
`import compression` 直接 ModuleNotFoundError。所以解压不能只靠标准库。

按「零依赖优先、逐级降级」排四级：

  1. `compression.zstd`（CPython ≥ 3.14，标准库，最优）
  2. `zstandard`（pip 装的第三方包，很多机器都有）
  3. `libzstd` 动态库 + ctypes（系统/软件自带的 .dll/.so，只做单次解压，用 ZSTD_decompress）
  4. `node --experimental-... zlib.zstdDecompressSync`（Node ≥ 22 内置 zstd）
     —— DSH 机器上几乎必有 Node（`dsh-runtimes` 里那份 24.x 也够）

只要命中任意一级就能解压。四级全不命中时，调用方收到 `ZstdUnavailable`，
由上层决定是「报错」还是「退化成不读正文」（本程序选择后者并明确告警）。

对外只暴露一个函数：

    decompress(data: bytes) -> bytes

和两个探针辅助：

    describe()      -> 人类可读的当前后端说明（写进 /api/health 与启动日志）
    available()     -> 是否至少有一个后端可用

关于第 4 级（Node）
-------------------

Node 一次调用只解压一份文件，进程启动约 30~60 ms。所以它**不是**逐文件调用用的：
backend.decode_many(files) 把一整批路径塞给 Node，一次进程解完整批，摊薄启动开销。
`decompress()` 是「单独解一份」的便捷入口，内部也走 decode_many。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile


class ZstdUnavailable(RuntimeError):
    """没有任何可用的 zstd 后端。"""


# ------------------------------------------------------------------ 后端实现
#
# 每个后端是一个 dict：
#   name        : 短名，写日志用
#   many        : 是否支持批量（一次调用解多个文件）
#   decode_many : ({路径: 原始字节}, 路径列表) -> {路径: 解压后字节}
# 批量后端的存在让 Node 那条慢路径变成「一次启动解完全部」，而不是每文件一次。

def _backend_stdlib():
    """CPython ≥ 3.14 的标准库 `compression.zstd`。"""
    try:
        from compression import zstd as _z          # noqa: F401
    except Exception:
        return None

    def decode_many(raw_map, paths):
        out = {}
        for p in paths:
            # 一份文件是**多帧拼接**的，`compression.zstd.decompress` 会一次
            # 解完全部帧（CPython 3.14 实测如此），直接用即可。
            out[p] = _z.decompress(raw_map[p])
        return out

    return {'name': 'compression.zstd（标准库，Python≥3.14）', 'many': True,
            'decode_many': decode_many}


def _backend_zstandard():
    """pip 包 `zstandard`（很多环境自带）。"""
    try:
        import zstandard as _z
    except Exception:
        return None
    dctx = _z.ZstdDecompressor()

    def decode_many(raw_map, paths):
        out = {}
        for p in paths:
            # 一份文件是**多帧拼接**的：ZstdDecompressor.decompress() 只解第一帧，
            # 必须用流式接口才能一路解到文件尾。
            with dctx.stream_reader(raw_map[p]) as r:
                out[p] = r.read()
        return out

    return {'name': 'zstandard（第三方 pip 包）', 'many': True, 'decode_many': decode_many}


def _backend_libzstd():
    """系统自带的 libzstd 动态库，用 ctypes 调 ZSTD_decompress。

    只做**单次**解压（ZSTD_decompress 要求输出缓冲已知），所以需要先问
    ZSTD_getFrameContentSize；返回 UNKNOWN(负数) 时放弃本后端 —— 多帧拼接的流
    本来就该走别的后端。

    这里不自己实现 zstd 解码器：一个完整的 zstd 解码器（FSE + Huffman + 序列执行）
    约 900 行，属于「自己写编译器」级别的风险，一旦有 bug 就是静默算错数；
    而 libzstd 在 Windows（Git for Windows 的 mingw64/bin）和 Linux/macOS 上
    几乎必然存在。找不到就落到 Node 那一级。
    """
    try:
        import ctypes
        import ctypes.util
    except Exception:
        return None

    cands = []
    if os.name == 'nt':
        # Git for Windows / MSYS2 常见位置；DSH 本机命中 D:\git\mingw64\bin
        for env in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, 'Git', 'mingw64', 'bin', 'libzstd.dll'))
        for drv in 'CDEFG':
            cands.append(drv + r':\git\mingw64\bin\libzstd.dll')
            cands.append(drv + r':\msys64\mingw64\bin\libzstd.dll')
        cands.append('libzstd.dll')
    else:
        for n in ('libzstd.so.1', 'libzstd.so', 'libzstd.dylib',
                  'libzstd.1.dylib', 'libzstd.1.5.dylib'):
            cands.append(n)
        found = ctypes.util.find_library('zstd')
        if found:
            cands.append(found)

    lib = None
    used = None
    for c in cands:
        try:
            lib = ctypes.CDLL(c)
            used = c
            break
        except Exception:
            continue
    if lib is None:
        return None
    try:
        sz = ctypes.c_size_t
        lib.ZSTD_decompress.restype = sz
        lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, sz, ctypes.c_void_p, sz]
        lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, sz]
        # 一帧压缩后占多少字节 —— 用来在「一个文件里拼了多帧」时逐帧走
        lib.ZSTD_findFrameCompressedSize.restype = sz
        lib.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_void_p, sz]
        # 内容大小未知时（DSH 的帧就不写 FCS）用它拿上界
        lib.ZSTD_decompressBound.restype = ctypes.c_ulonglong
        lib.ZSTD_decompressBound.argtypes = [ctypes.c_void_p, sz]
        lib.ZSTD_isError.restype = ctypes.c_uint
        lib.ZSTD_isError.argtypes = [sz]
        lib.ZSTD_getErrorName.restype = ctypes.c_char_p
        lib.ZSTD_getErrorName.argtypes = [sz]
    except Exception:
        return None

    # ZSTD_CONTENTSIZE_UNKNOWN = 2^64-1，ZSTD_CONTENTSIZE_ERROR = 2^64-2
    UNKNOWN = (1 << 64) - 1
    ERROR = (1 << 64) - 2

    def _decode_one(data, path):
        """解一个文件。

        **一个 .zstd 文件里拼了很多个独立的 zstd 帧**：DSH 每次持久化只追加一小帧，
        646 帧拼成一份完整会话日志。这一步必须处理对，否则数字会离谱：

          * `ZSTD_decompress` 本身就把**输入里的所有帧**解完再返回（一次调用、
            输出是全部帧拼接的结果）——所以**只调一次**，不要按帧循环；
          * 按帧循环反而会把整份文件解 646 遍（输出 1.1 GB，实测踩到）。
        """
        src = ctypes.create_string_buffer(data, len(data))
        n = len(data)
        fcs = lib.ZSTD_getFrameContentSize(src, n)
        if fcs == ERROR:
            raise ZstdUnavailable('libzstd 读帧头失败（%s）' % path)
        cap = fcs if fcs != UNKNOWN else lib.ZSTD_decompressBound(src, n)
        if cap == UNKNOWN or cap >= ERROR or cap <= 0:
            raise ZstdUnavailable('libzstd 无法估算帧大小（%s）' % path)
        dst = ctypes.create_string_buffer(int(cap))
        got = lib.ZSTD_decompress(dst, cap, src, n)
        if lib.ZSTD_isError(got):
            raise ZstdUnavailable('libzstd 解压失败：%s（%s）'
                                  % (lib.ZSTD_getErrorName(got).decode('utf-8', 'replace'), path))
        return dst.raw[:got]

    def decode_many(raw_map, paths):
        return {p: _decode_one(raw_map[p], p) for p in paths}

    return {'name': 'libzstd + ctypes（%s）' % used, 'many': True, 'decode_many': decode_many}


_NODE_CANDIDATES = (
    'node',
    os.path.join('~', '.dsh', 'dsh-runtimes'),     # 占位，下面展开
)


def _find_node():
    """找一个支持 zlib.zstdDecompressSync 的 node.exe。

    顺序：PATH 上的 node → DSH 托管的运行时（`$DSH_HOME/dsh-runtimes/*/dependencies/node/bin`）
    → 常见安装目录。都要求 Node ≥ 22.15（zlib.zstd* 的落地版本）。
    """
    cands = []
    exe = shutil.which('node')
    if exe:
        cands.append(exe)
    home = os.environ.get('DSH_HOME')
    if not home:
        home = os.path.join(os.path.expanduser('~'), '.dsh')
    rt = os.path.join(home, 'dsh-runtimes')
    try:
        for name in sorted(os.listdir(rt)):
            for sub in (('dependencies', 'node', 'bin', 'node.exe'),
                        ('dependencies', 'node', 'node.exe')):
                p = os.path.join(rt, name, *sub)
                if os.path.isfile(p):
                    cands.append(p)
    except Exception:
        pass
    if os.name == 'nt':
        for drv in 'CDEFG':
            cands.append(drv + r':\node\node.exe')
            cands.append(drv + r':\Program Files\nodejs\node.exe')
    seen = set()
    for c in cands:
        try:
            c = os.path.abspath(c)
        except Exception:
            continue
        if c in seen or not os.path.isfile(c):
            continue
        seen.add(c)
        try:
            # 只测「有没有 zstdDecompressSync」，不真的解压
            r = subprocess.run([c, '-e', "process.stdout.write(typeof require('node:zlib').zstdDecompressSync)"],
                               capture_output=True, timeout=20)
            if r.returncode == 0 and r.stdout.strip() == b'function':
                return c
        except Exception:
            continue
    return None


def _backend_node():
    """Node ≥ 22.15 的 `zlib.zstdDecompressSync`，一次进程解一整批。"""
    node = _find_node()
    if not node:
        return None

    # 脚本从 stdin 读 JSON {path: base64}，往 stdout 写 JSON {path: base64}。
    # 传的是**字节**而不是路径：这样 decompress() 的内存数据也能走这一级，
    # 而且不受「工作目录不同 / 文件被占」影响。不用命令行参数是因为 40 个
    # base64 串会顶到 Windows 的 8K 命令行上限。
    #
    # 关键：**一份文件里拼了几百个 zstd 帧**，而 zstdDecompressSync 只解第一帧
    # （实测 1201003 字节的日志只解出 200 字节）。这里先用 zstdDecompressSync 试，
    # 结果明显偏短（或抛错）就换成流式接口 zstdDecompress 逐块读完。
    script = r"""
const zlib = require('node:zlib');
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', () => {
  const MAX = 2147483647;            // Node 允许的最大值；会话日志解压后远小于它
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);   // zstd 帧魔数（小端）
  // **必须逐帧解**：Node 的 zstdDecompressSync / createZstdDecompress 只解第一帧
  // （实测 1201003 字节的日志只吐 200 字节），所以按魔数切开自己拼。
  // 这跟 libzstd 的 ZSTD_decompress 不同 —— 后者一次就把所有帧解完。
  function frameByFrame(buf) {
    const parts = [];
    let off = 0;
    while (off < buf.length) {
      const next = buf.indexOf(MAGIC, off + 4);
      const end = next === -1 ? buf.length : next;
      parts.push(zlib.zstdDecompressSync(buf.subarray(off, end), { maxOutputLength: MAX }));
      off = end;
    }
    return Buffer.concat(parts);
  }
  const out = {};
  const errors = {};
  for (const p of Object.keys(input.frames)) {
    try {
      out[p] = frameByFrame(Buffer.from(input.frames[p], 'base64')).toString('base64');
    } catch (e) {
      errors[p] = String(e && e.message || e);
    }
  }
  process.stdout.write(JSON.stringify({ ok: out, errors }));
});
"""

    def decode_many(raw_map, paths):
        import base64
        payload = json.dumps({'frames': {p: base64.b64encode(raw_map[p]).decode('ascii')
                                         for p in paths}}).encode('utf-8')
        r = subprocess.run([node, '-e', script], input=payload,
                           capture_output=True, timeout=900)
        if r.returncode != 0:
            raise ZstdUnavailable('node 解压进程失败：%s'
                                  % r.stderr.decode('utf-8', 'replace')[:400])
        got = json.loads(r.stdout.decode('utf-8'))
        out = {}
        for p, b64 in got.get('ok', {}).items():
            out[p] = base64.b64decode(b64)
        if got.get('errors'):
            p, msg = next(iter(got['errors'].items()))
            raise ZstdUnavailable('node 解压失败：%s（%s）' % (msg, p))
        return out

    return {'name': 'node zlib.zstdDecompressSync（%s）' % node, 'many': True,
            'decode_many': decode_many}


_BACKENDS = None
_ACTIVE = {'name': None, 'why': None}


def _backends():
    """按优先级列出可用后端（懒加载，只探测一次）。"""
    global _BACKENDS
    if _BACKENDS is not None:
        return _BACKENDS
    # 允许强制指定后端，排障用：DSH_TOKEN_ZSTD=stdlib|zstandard|libzstd|node
    want = (os.environ.get('DSH_TOKEN_ZSTD') or '').strip().lower()
    factories = [('stdlib', _backend_stdlib), ('zstandard', _backend_zstandard),
                 ('libzstd', _backend_libzstd), ('node', _backend_node)]
    order = [f for k, f in factories if not want or want == k]
    out = []
    for f in order:
        try:
            b = f()
        except Exception:
            b = None
        if b:
            out.append(b)
    _BACKENDS = out
    return out


def available():
    return bool(_backends())


def describe():
    """当前会用的后端说明。"""
    bs = _backends()
    if not bs:
        return '不可用（没有 compression.zstd / zstandard / libzstd / node 的任何一级）'
    return bs[0]['name']


def decode_many(raw_map):
    """批量解压。raw_map = {路径: 压缩字节}，返回 {路径: 解压字节}。

    逐级降级：某个后端在**实际解压**时抛错（例如 libzstd 遇到未知帧大小），
    就换下一级重试，而不是直接把整轮扫描判死。
    """
    if not raw_map:
        return {}
    paths = list(raw_map)
    errs = []
    for b in _backends():
        try:
            out = b['decode_many'](raw_map, paths)
            _ACTIVE['name'] = b['name']
            _ACTIVE['why'] = None
            return out
        except ZstdUnavailable as e:
            errs.append('%s: %s' % (b['name'], e))
            continue
        except Exception as e:
            errs.append('%s: %r' % (b['name'], e))
            continue
    raise ZstdUnavailable('所有 zstd 后端都失败：\n  ' + '\n  '.join(errs) if errs
                          else '没有可用的 zstd 后端')


def decompress(data):
    """单份解压（内部走一次批量调用，主要给测试和命令行用）。"""
    key = '<memory>'
    return decode_many({key: data})[key]


def active():
    """最近一次成功解压用的后端名（没解过则为 None）。"""
    return _ACTIVE['name']


if __name__ == '__main__':
    # 自检：python zstd_bridge.py <某个 .zstd 文件>
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        with open(sys.argv[1], 'rb') as fh:
            raw = fh.read()
        print('后端：%s' % describe())
        print('压缩 %d 字节 → 解压 %d 字节' % (len(raw), len(decompress(raw))))
    else:
        print('后端：%s' % describe())
        print('可用：%s' % available())
