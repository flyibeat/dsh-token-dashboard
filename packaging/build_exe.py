# -*- coding: utf-8 -*-
"""把 usage_server.py + zstd_bridge.py + dashboard.html 打成单文件 exe。

用法（必须先装好 PyInstaller：pip install pyinstaller）：
    python packaging/build_exe.py

产物：
    dist/dsh-usage.exe          单文件，拷走即用
    packaging/_build/ , dsh-usage.spec   PyInstaller 中间产物，可随时删

设计要点：
    * dashboard.html 用 --add-data 打进 exe 作**兜底**；程序目录下若另有一个
      dashboard.html，那个优先（保留热加载，改前端不必重新打包）。
    * 只打包运行态需要的文件，不把日志、缓存、验证脚本卷进去。
    * 剔除 tkinter/unittest 等用不到的重模块压体积。
    * **不打包 zstd 后端**：libzstd / node / compression.zstd 都是运行时探测的，
      打进 exe 反而会把「用哪个后端」锁死、还要多背一份体积。
      exe 所在机器只要满足 zstd_bridge 四级里任意一级即可（绝大多数机器都满足）。
"""
import os
import subprocess
import sys
import time

# 英文版 Windows 的控制台代码页是 cp1252，直接 print 中文会 UnicodeEncodeError 崩掉。
# 统一切到 UTF-8，编不出来就替换。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.dirname(HERE)
ENTRY = os.path.join(SRV, 'usage_server.py')
BRIDGE = os.path.join(SRV, 'zstd_bridge.py')
HTML = os.path.join(SRV, 'dashboard.html')
DIST = os.path.join(SRV, 'dist')
BUILD = os.path.join(HERE, '_build')
NAME = 'dsh-usage'

EXCLUDES = [
    'tkinter', 'unittest', 'pydoc', 'doctest', 'lib2to3',
    'test', 'distutils', 'setuptools', 'pip', 'pkg_resources',
]
# 注意：不要排除 email / html —— http.server 内部要用它们（格式化 Date 头、转义错误页）。
# 也不要排除 xml、urllib 子模块，收益小、踩坑概率高。


def _candidate_interpreters():
    """装了 PyInstaller 的解释器候选。

    为什么要探测而不是写死：本脚本用 sys.executable 去跑 `-m PyInstaller`，谁调用它就用谁——
    而 DSH 托管的 Python（`$DSH_HOME/dsh-runtimes/*/dependencies/python`）**没装** PyInstaller，
    于是打包静默失败。这里改成「当前解释器不行就自动换一个能用的」。

    顺序：DSH_PYTHON 环境变量 → PATH 里的 python/py。不写死用户名或盘符。
    """
    cands = [sys.executable]
    env_py = os.environ.get('DSH_PYTHON', '').strip()
    if env_py:
        cands.append(env_py)
    # DSH 托管的运行时（可能有 pip 装过 PyInstaller）
    home = os.environ.get('DSH_HOME') or os.path.join(os.path.expanduser('~'), '.dsh')
    rt = os.path.join(home, 'dsh-runtimes')
    try:
        for name in sorted(os.listdir(rt)):
            cands.append(os.path.join(rt, name, 'dependencies', 'python', 'python.exe'))
            cands.append(os.path.join(rt, name, 'dependencies', 'python', 'bin', 'python'))
    except Exception:
        pass
    cands += ['python', 'python3', 'py', 'py -3']
    return cands


def pick_interpreter():
    """返回一个能 `import PyInstaller` 的解释器路径；找不到就返回 None。"""
    seen, out = set(), []
    for p in _candidate_interpreters():
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    for p in out:
        # 绝对路径的候选先确认文件在；裸命令（python/py）交给 PATH 解析
        if os.path.isabs(p) and not os.path.exists(p):
            continue
        try:
            r = subprocess.run(p.split() + ['-c', 'import PyInstaller'],
                               capture_output=True, timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            return p
    return None


def main():
    for f, label in ((ENTRY, '入口脚本'), (BRIDGE, 'zstd 桥接'), (HTML, '前端')):
        if not os.path.exists(f):
            print('缺少%s：%s' % (label, f))
            return 2

    py = pick_interpreter()
    if not py:
        print('找不到装有 PyInstaller 的解释器。已试：')
        for p in _candidate_interpreters():
            print('  ' + p)
        print('请先安装：<上面某个 python> -m pip install pyinstaller')
        return 3
    if py != sys.executable:
        print('当前解释器没有 PyInstaller，自动切换到：%s' % py)
    print('构建解释器：%s' % py)

    args = py.split() + ['-m', 'PyInstaller',
        '--onefile',
        '--console',
        '--name', NAME,
        '--distpath', DIST,
        '--workpath', BUILD,
        '--specpath', HERE,
        '--noconfirm',
        '--clean',
        # Windows 上 os.pathsep 是 ';'，POSIX 是 ':' —— 交给 os.pathsep 自适应
        '--add-data', HTML + os.pathsep + '.',
    ]
    for mod in EXCLUDES:
        args += ['--exclude-module', mod]
    args.append(ENTRY)

    print('即将执行：')
    print('  ' + ' '.join('"%s"' % a if ' ' in a else a for a in args))
    print()
    t0 = time.time()
    r = subprocess.run(args, cwd=HERE)
    dt = time.time() - t0
    print()
    print('PyInstaller 退出码 %d，耗时 %.1f 秒' % (r.returncode, dt))
    if r.returncode != 0:
        return r.returncode

    exe = os.path.join(DIST, NAME + '.exe')
    if not os.path.exists(exe):
        exe = os.path.join(DIST, NAME)          # 非 Windows
    if os.path.exists(exe):
        print('产物：%s  (%.1f MB)' % (exe, os.path.getsize(exe) / 1048576.0))
    else:
        print('未找到产物，请检查 dist 目录：%s' % DIST)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
