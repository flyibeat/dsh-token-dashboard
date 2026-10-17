# DeepSeek Harness 词元用量看板

一个**零第三方依赖**的本地用量看板：单文件 Python 后端 + 单文件 HTML 前端，只读解析
DeepSeek Harness（dsh）的会话记录，聚合成按天 / 月 / 模型 / 供应商 / 会话 / 小时的词元用量视图。

```
usage_server.py   ← 后端：增量扫描 + 聚合 + HTTP 服务（纯标准库）
zstd_bridge.py    ← zstd 解压的四级降级桥（DSH 的会话日志是 zstd 压缩的）
dashboard.html    ← 前端：单文件看板，服务按 mtime 热加载
selfcheck.py      ← 自检脚本：不开服务，直接验后端关键行为
packaging/        ← 打包成单文件 exe
start.cmd/.sh     ← 一键启动
```

## 特性

| 特性 | 说明 |
|---|---|
| **零依赖** | Python 侧只用标准库（`http.server` / `json` / `collections`）。zstd 解压走四级降级，见下文 |
| **增量扫描** | 按 `[mtime, size]` 签名缓存，只重解析变动的文件。首次全量约 1 秒，之后近乎瞬时 |
| **前端热加载** | `dashboard.html` 是独立文件。改完存盘即可，页面 30 秒内自动重载，不用重启后端 |
| **只读且克制** | **从不读取会话正文**，只取用量计数、供应商/模型名、标题与工作目录 |
| **跨平台** | Windows / macOS / Linux 通用。数据根四级 fallback，以服务 / 计划任务等非交互身份运行也能自动定位 |
| **可打包** | 一条命令打成单文件 exe |
| **默认只听本机** | 默认绑定 `127.0.0.1`，不对外暴露 |
| **自带自检** | `selfcheck.py` 不开服务就能验解析口径、数据根定位与聚合正确性 |

## 快速开始

**前置条件**：本机装过 dsh，且 `~/.dsh/sessions/` 下有会话数据。

```bash
# Windows：双击 start.cmd
# macOS / Linux：./start.sh
# 或者手动：
python usage_server.py
```

然后浏览器打开 <http://127.0.0.1:8793>。

打开时加了 `--open` 会自动拉起浏览器。后台常驻时不要加。

## 数据来源

程序只读两处，**全部位于 dsh 的数据目录**（默认 `~/.dsh`）：

| 来源 | 用途 | 取哪些字段 |
|---|---|---|
| `sessions/<工作区>/<会话ID>/session.v<N>.jsonl.zstd` | 逐条累计用量 | 只取 `assistant/message` 事件的 `data.usage`（`inputTokens` / `outputTokens` / `cacheReadTokens` / `cacheWriteTokens`）、`data.message.source`（`provider` / `model`）、`time` |
| `storages/session_projcache/sessions/<会话ID>.json` | 补会话标题与工作目录 | `record.rows.title.val`、`record.identity.cwd`。**不读 `titleInput` 等含正文的键** |

不需要装任何插件，也不读取第三方插件的任何文件。

> **缓存里存什么**：`scan-cache.json` 只存 `{绝对路径: {"sig": [mtime, size], "recs": [[数字…]]}}` ——
> 纯数字数组，**连会话标题与 cwd 都不落盘**。

### 数据根定位（四级 fallback）

程序不硬编码数据目录，按以下顺序定位；这解决了「以服务 / 计划任务等非交互身份运行时读不到数据」的经典问题：

| 级 | 条件 | 来源标记 |
|---|---|---|
| 1 | 设了 `DSH_USAGE_HOME`（或命令行 `--home`） | `DSH_USAGE_HOME` |
| 2 | 设了 `DSH_HOME`（dsh 自己就用这个） | `DSH_HOME` |
| 3 | `~/.dsh` 存在 | `用户目录` |
| 4 | 以上都不成立 → 自动探测 `<盘>:\Users\*\.dsh`（POSIX 为 `/Users/*`、`/home/*`）。命中多个时先按 `USERNAME` 同名择优，再按会话文件数择优 | `自动探测（唯一命中）` / `自动探测（按用户名）` / `自动探测（按数据量）` |
| 5 | 都没找到 | 回退并打印自检块 |

扫到 0 个文件时会打印**自检块**：选中的数据根、判定来源、程序目录、相关环境变量、探测过的全部候选项
（标注「有数据 / 不存在」）以及两种修法。**不会静默返回空数据。**

## 关于 zstd：为什么要一个四级降级的解压桥

dsh 把会话事件流写成 `session.v<N>.jsonl.zstd`。两点注意：

1. **是 zstd，不是 gzip**。纯标准库的 Python 只有 **3.14 起**才有 `compression.zstd`；
   而 dsh 自己托管的 Python 运行时是 **3.12**（`$DSH_HOME/dsh-runtimes/*/dependencies/python`），
   `import compression` 直接 `ModuleNotFoundError`。
2. **一个文件里拼了很多个独立的 zstd 帧**。dsh 每次持久化只追加一小帧，本机实测一份
   1.2 MB 的日志里拼了 **646 帧**。

所以 `zstd_bridge.py` 按「零依赖优先、逐级降级」排四级，命中任意一级就能用：

| 级 | 后端 | 说明 |
|---|---|---|
| 1 | `compression.zstd` | CPython ≥ 3.14 的标准库，最优 |
| 2 | `zstandard` | pip 装的第三方包，很多机器都有 |
| 3 | `libzstd` + `ctypes` | 系统/软件自带的动态库。Windows 上常见于 Git for Windows 的 `mingw64\bin\libzstd.dll`；POSIX 找 `libzstd.so*` / `libzstd.dylib` |
| 4 | `node` 的 `zlib.zstdDecompressSync` | Node ≥ 22.15。**dsh 机器上几乎必有 Node**（`dsh-runtimes` 里那份 24.x 就够），所以这是最可靠的兜底 |

**踩过的坑（三级后端的多帧行为完全不同，处理错会算出离谱的数）**：

- `ZSTD_decompress`（libzstd）**一次调用就把输入里的所有帧解完**。第一版按帧循环调用，
  结果把 1.2 MB 的文件解了 646 遍、输出 1.17 GB。正确做法是只调一次。
- Node 的 `zstdDecompressSync` / `createZstdDecompress` **只解第一帧就停**（实测 1.2 MB 只吐 200 字节），
  必须按帧魔数 `28 B5 2F FD` 切开逐帧解再拼。
- `ZstdDecompressor.decompress()`（`zstandard` 包）同样只解单帧，要用 `stream_reader`。
- dsh 写的帧**不带 `Frame_Content_Size`**（`ZSTD_getFrameContentSize` 返回 `UNKNOWN`），
  得先用 `ZSTD_decompressBound` 拿输出上界再分配缓冲。

四个后端在本机 40 个日志文件上做过交叉验证，解压结果**逐字节相同**
（`total_out = 29,376,483` 字节，sha256 一致）。

排障时可以强制指定后端：`DSH_TOKEN_ZSTD=stdlib|zstandard|libzstd|node`。

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DSH_USAGE_HOME` | （空） | 显式指定 dsh 数据目录。可指向 `~/.dsh` 本身或其上级 |
| `DSH_HOME` | （空） | dsh 自己的数据根，程序会自动识别 |
| `DSH_USAGE_PORT` | `8793` | 监听端口 |
| `DSH_USAGE_BIND` | `127.0.0.1` | 绑定地址。**改成 `0.0.0.0` 会让同网段任何人都能读到你的会话标题与工作目录** |
| `DSH_USAGE_REFRESH` | `60` | 后台重扫间隔（秒） |
| `DSH_USAGE_META_TTL` | `300` | 会话标题/目录元数据的缓存时长（秒） |
| `DSH_TOKEN_ZSTD` | （空） | 强制指定 zstd 后端，排障用 |

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--port N` | 临时换端口，等价于 `DSH_USAGE_PORT` |
| `--home PATH` | 临时指定数据目录，等价于 `DSH_USAGE_HOME` |
| `--open` | 启动后自动打开浏览器（后台常驻时不要加） |
| `--rebuild` | 忽略缓存，强制全量重扫 |
| `--version` | 只打印版本号后退出 |

带值的参数两种写法都认：`--port 8800` 与 `--port=8800` 等价。

## HTTP 接口

| 路径 | 说明 |
|---|---|
| `GET /` | 看板页面。优先用程序目录下的 `dashboard.html`，没有则用内嵌副本 |
| `GET /api/data` | 完整聚合数据（`days` / `months` / `models` / `sessions` / `hours` / `recordCount` / `source` / `span` / `summary` / `htmlMtime` / `serverVersion`）。带 `?force=1` 触发立即重扫 |
| `GET /api/health` | 运行状态（`records` / `files` / `error` / `scanSeconds` / `dataRoot` / `dataRootSource` / `zstdBackend` / `sessionMetaOk` / `version`） |

**没有内置鉴权。** 安全性依赖默认只绑 `127.0.0.1`。若要对外提供，请在前面套一层带鉴权的反向代理。

## 统计口径

### 四个词元桶（互不重叠，加起来就是计费输入 + 输出）

| 桶 | 含义 |
|---|---|
| **未缓存输入**（fresh） | `usage.inputTokens` —— 真正按新输入计价的输入词元 |
| **缓存命中**（cached） | `usage.cacheReadTokens` —— 命中提示缓存的输入词元（便宜） |
| **缓存写入**（cwrite） | `usage.cacheWriteTokens` —— 写入缓存的输入词元（部分供应商按略高于普通输入计价） |
| **输出**（output） | `usage.outputTokens` —— reasoning 已含在里面 |

- **总词元** = 未缓存输入 + 缓存命中 + 缓存写入 + 输出
- **输入合计**（`inputTotal`）= 未缓存输入 + 缓存命中 + 缓存写入，是**缓存命中率的分母**
- **请求数**（req）按 `assistant/message` 事件条数计，一条消息 = 一次计费的模型调用

> **一个容易搞错的地方**：dsh 的 `usage.inputTokens` **本身就是未命中缓存的输入**，
> 不像有些平台把「输入合计」塞在这个字段里、再用一个命中数去减。所以这里
> **没有** `input − cached` 这种减法 —— 减了就会把数字算错。

### 聚合维度

后端一次扫出、全部塞进 `/api/data`：按天 `days`、按月 `months`、按
**供应商+模型** `models`、按会话 `sessions`、按小时 `hours`。

模型维度带 `provider` 因为**同一模型可能挂在多个供应商下**（本机实测 `gpt-6-astra`
就同时出现在 `syngnat` 和别处），只看模型名会把两个通道的数字糊在一起。

### 同一个会话有多份日志时，只取最高版本

dsh 的会话目录里可能同时留着 `session.v3.jsonl.zstd` 和 `session.v4.jsonl.zstd` ——
它们是**同一次会话在不同格式版本下的副本**，不是两段用量。程序对每个会话目录只取
版本号最高的那份。本机 39 个会话目录里有 1 个同时有 v3+v4，不去重就会重复累加。

## 口径怎么自证

不靠「我觉得」。`selfcheck.py` 把解析口径、数据根定位、聚合一致性都做成了断言，
不开服务就能跑：

```bash
python selfcheck.py
# OK=38  FAIL=0
```

其中两条最关键：**四个词元桶之和恒等于 total**，以及**天／模型／月三个维度的总量互相一致**。
再加一条独立旁证 —— dsh 自己在 `storages/session_projcache/sessions/<会话ID>.json` 的
`record.rows.tokenUsage.val.totals` 里存了每会话用量（由 dsh 的 `dsh-token-meter` 折出来，
和本项目的解析代码毫无关系），拿它逐会话比一遍即可确认读法正确。

## 关于打包

`packaging/build_exe.py` 是 PyInstaller 的 onefile 封装，已排除
`tkinter` / `unittest` / `pydoc` / `doctest` / `lib2to3` / `test` / `distutils` / `setuptools` / `pip` 等无用模块。

```bash
pip install pyinstaller
python packaging/build_exe.py
# 产物：dist/dsh-usage.exe，拷到任意目录直接双击
```

设计要点：`dashboard.html` 用 `--add-data` 打进 exe 作**兜底**；程序目录下若另有
`dashboard.html`，那个优先 —— 这样打包后仍能热改前端，不必重新打包。

**不打包 zstd 后端**：`libzstd` / `node` / `compression.zstd` 都是运行时探测的，
打进 exe 反而会把「用哪个后端」锁死、还要多背一份体积。目标机只要满足四级里任意一级即可。

## 让它常驻后台

程序本身是前台进程，关掉窗口即退出。想让它一直跑，用任何进程管理器挂它都行，不需要改代码：

- **Windows**：用 [NSSM](https://nssm.cc/) 注册成服务 —— `nssm install dsh-usage "C:\path\to\dsh-usage.exe"`，
  然后把启动目录（AppDirectory）设成 exe 所在目录。注意服务账户的 `USERPROFILE` 与你的交互账户不同，
  程序会按四级 fallback 自动探测数据目录；探测不准时用 `DSH_USAGE_HOME` 显式指定。
- **macOS**：写个 `launchd` plist，或干脆 `nohup ./dsh-usage &`
- **Linux**：写个 systemd user unit

两个坑值得留意：

1. **「已停止」不等于进程已经退出。** 端口可能还被上一次的进程占着，此时覆盖文件会成功，
   但新实例绑不上端口而直接退出 —— 表面上「重启成功」，实际跑的还是旧版本。
2. **「进程在运行」不等于「数据正常」。** 请以 `/api/health` 的 `records > 0` 为准，而不是以进程状态为准。

## 排错

### 页面能打开，但所有数字都是 0

**先看 `/api/health` 的 `dataRoot` 有没有展开成真实路径**，别急着查端口：

```bash
curl http://127.0.0.1:8793/api/health
# records=0 且 error 为 null，同时 files=0  → 数据根没定位对
# zstdBackend 显示「不可用」              → 没有 zstd 后端，见下一节
```

若日志里出现自检块，它已经列出了全部候选项和修法。

### 报 zstd 不可用 / 解压失败

看 `/api/health` 的 `zstdBackend`。若显示「不可用」，任选一条修：

```bash
pip install zstandard                     # 最省事
# 或让 PATH 上有 node（≥22.15）
# 或确保系统有 libzstd（Windows 上装 Git for Windows 即可）
```

### 端口被占用

后端会打一条日志后以非零码退出。用 `--port` 换口，或先找出占用者：

```powershell
Get-NetTCPConnection -LocalPort 8793 -State Listen | ForEach-Object {
  Get-Process -Id $_.OwningProcess
}
```

### 改了代码却不生效

先确认端口真正释放了，再谈「重启」。被其他进程托管时尤其容易踩：停止托管进程不一定终止
它拉起的子进程，上次的实例还在服务旧代码，新实例因端口被占直接退出。

## 隐私说明

- 只读，不修改 dsh 的任何文件。
- **不读取会话正文**：只取 `assistant/message` 的 `usage` 与 `source`，以及
  projcache 里的「标题」「工作目录」两个字段；`titleInput`（含首条提问原文）等键一律不读。
- 磁盘缓存 `scan-cache.json` 只存**纯数字**数组与文件路径，不存标题、不存工作目录。
- 无任何遥测与外部请求：源码中的 URL 只有 `http://127.0.0.1:*`，不访问互联网。
- 无内置鉴权，默认只绑 `127.0.0.1`。

## 与 WorkBuddy 版的关系

本项目是照着「WorkBuddy 词元用量看板」的做法，为 dsh 重做的一版。相同的是整体形态
（零依赖单文件后端 + 单文件前端 + 增量扫描 + 热加载 + 可打包 + 只读克制）。

不同的是**数据层整个换掉了**：

| | WorkBuddy 版 | 本项目（dsh） |
|---|---|---|
| 日志格式 | 明文 JSONL | **zstd 压缩**，且一个文件拼了几百帧 → 需要 `zstd_bridge.py` |
| 会话元数据 | `workbuddy.db` 的 `sessions` 表（sqlite 只读 URI） | `storages/session_projcache/*.json`（纯 JSON，不需要 sqlite） |
| 用量字段 | `providerData.usage` + `rawUsage` + `credit` 积分 | `data.usage` 的四个词元桶，**没有积分概念** |
| 缓存命中字段 | `prompt_cache_hit_tokens`，另有 `prompt_tokens_details.cached_tokens` 兜底 | `cacheReadTokens` / `cacheWriteTokens` 两个独立字段 |
| 模型名 | 三个字段来回漂移，还要剥 `custom-local:` 前缀、识别 `auto` 等档位词 | `message.source.model`，直取即可 |
| 积分/计费 | 有「积分」口径（1 积分 ≈ ¥0.07） | **已移除** —— dsh 不记积分，也不在转录里记费用 |
| 数据根 | `~/.workbuddy` | `~/.dsh`（或 `$DSH_HOME`） |

积分相关的卡片、列、排序键全部删掉了 —— dsh 转录里没有这个概念，留着只会显示一列恒为 0
的假数据。

对账相关的东西也一并删掉了：本项目只依赖 dsh 自己的会话转录，**不假设你装了任何第三方插件**。

## License

MIT。
