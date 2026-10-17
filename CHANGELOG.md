# 版本记录 — DeepSeek Harness 词元用量看板

> 本项目的**已记录**变更都在这留一条。
>
> **记录时机**：**不逐次记录**。日常改动只落盘 + 往 `PENDING.md` 记一行；
> 等用户明确要求「发版 / 更新」时，才把这段时间的改动**合并成一条**写在这里。
>
> 版本号规则（语义化）：`MAJOR.MINOR.PATCH`
> - `MINOR` — 新功能 / 新需求（用户提出的能力变更）
> - `PATCH` — 修复 / 口径纠正 / 文案调整，不改能力边界
> - `MAJOR` — 架构级重写（数据源、交付形态发生根本变化）
>
> **发行标签形态**：`vX.Y.Z`，且 X / Y / Z **各只占一位数字** —— 到 9 就进位：
> `v1.0.9` 的下一个是 `v1.1.0`（不是 `v1.0.10`）。
>
> 三套版本互相独立：前端 `FE_VER`（dashboard.html）、后端 `BE_VER`（usage_server.py）、
> 发行版本（git 标签 / Release）。

开发目录（本工作区）：`<项目工作空间>\dsh-token-dashboard\`
实际运行目录：`D:\dsh_token_usage\`　｜　服务名：`dsh-usage-web`（NSSM）　｜　访问 `http://127.0.0.1:8793`

---

## v1.0.0 — 2026-10-18 ｜ 首个版本

**类型**：MAJOR（新建项目）

按「WorkBuddy 词元用量看板」的形态，为 DeepSeek Harness（dsh）重做的本地用量看板。
零第三方依赖、单文件后端 + 单文件前端、只读解析会话转录。

**数据层（与 WorkBuddy 版完全不同的部分）**：

- **会话日志是 zstd 压缩的**，而且**一份文件里拼了几百个独立 zstd 帧**
  （本机一份 1.2 MB 的日志 646 帧）→ 新增 `zstd_bridge.py`，四级降级解压：
  `compression.zstd`（Py≥3.14）→ `zstandard` → `libzstd`+ctypes → `node zlib`。
  四级在本机 40 个文件上交叉验证，解压结果**逐字节相同**。
- **会话目录是两层**（`sessions/<工作区>/<会话ID>/`），按一层扫会扫出 0 个文件。
- **同一会话目录可能同时留 `session.v3` 与 `session.v4`**（同一次会话的格式副本），
  只取最高版本，否则用量重复累加。
- 会话元数据改读 `storages/session_projcache/*.json`（纯 JSON，不需要 sqlite）。

**口径**（贯穿所有板块）：

| 桶 | 来源字段 | 含义 |
|---|---|---|
| 未缓存输入 fresh | `usage.inputTokens` | 真正按新输入计价的部分 |
| 缓存命中 cached | `usage.cacheReadTokens` | 命中提示缓存，便宜 |
| 缓存写入 cwrite | `usage.cacheWriteTokens` | 写入缓存 |
| 输出 output | `usage.outputTokens` | reasoning 已含在其中 |

- `total` = 四桶之和；`inputTotal` = fresh + cached + cwrite（缓存命中率的分母）。
- **dsh 的 `inputTokens` 本身就是「未缓存输入」**，不做 `input − cached` 减法。
- **积分口径整块移除** —— dsh 转录里没有这个概念。
- 模型维度带 `provider`：同一模型可能挂在不同供应商下。

**界面**：KPI 卡 / 词元构成图（四色堆叠、可横向滚动与滚轮缩放）/ 分时与每日明细 /
模型分布 / 单会话 Top 20 / 月度汇总。表头排序记忆、范围记忆、会话标题列宽可拖拽。

**对账与插件依赖**：**不依赖任何第三方插件**，只读 dsh 自己的会话转录。
早期开发阶段曾有「与 dsh-usage 账本对账」的页面提示与独立工具，已全部移除。

**质量**：`selfcheck.py` 38 项断言全过（解析容错、数据根四级定位、聚合一致性、
四个词元桶之和恒等于 total、天/模型/月三维度总量一致）。

**交付**：GitHub Actions 三平台构建（Windows / Linux / macOS），单文件产物。
本机以 NSSM 注册为 `dsh-usage-web` 服务常驻。
