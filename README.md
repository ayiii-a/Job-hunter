# Job Hunting Agent

面向个人的求职流水线：**岗位监控 → JD 分析 → 简历定制 → 申请表起草 → 投递追踪 → 邮件处理**。

人在回路（human-in-the-loop）：数据留在本机，对外动作由人确认。模型负责判断和起草，确定性代码负责执行和校验。

| | |
|---|---|
| 运行环境 | Python 3.11+（Windows / macOS / Linux） |
| 存储 | 本地 SQLite + 本地 YAML 配置 |
| 模型 | Anthropic Claude（编排用 Sonnet，批量活用 Haiku） |
| 状态 | Phase 0–5 与 agent loop 已完成；OpenClaw 外壳代码侧完成、待实机验证；面试模拟未实现 |
| 设计文档 | [job_hunting_agent_roadmap.md](job_hunting_agent_roadmap.md) |

> **仓库里只有模板。** 母简历包含真实姓名、电话、住址和完整履历，所以 `config/*.yaml`、`.env`、`data/` 全部在 `.gitignore` 中，仓库只保留 `*.example.yaml`。代价是配置没有 git 版本历史，需要自行备份。

---

## 目录

- [功能](#功能)
- [设计原则](#设计原则)
- [快速开始](#快速开始)
- [配置](#配置)
- [命令参考](#命令参考)
- [自动化运行](#自动化运行)
- [安全模型](#安全模型)
- [成本控制](#成本控制)
- [开发](#开发)
- [项目结构](#项目结构)
- [路线图](#路线图)
- [免责声明](#免责声明)

---

## 功能

| 模块 | 能力 | 入口 |
|---|---|---|
| 岗位监控 | 从 Greenhouse / Lever / Ashby 官方接口抓取，按地点、标题、职级规则初筛后入库 | `agent fetch` |
| JD 分析 | 逐条分档（strong_apply / apply / stretch / skip），提取技能要求、年限、gap | `agent analyze` |
| 简历定制 | 按 bullet id 从母简历选材、按 JD 关键词改写、渲染 PDF、一页约束、幻觉校验 | `agent tailor <job_id>` |
| 申请表起草 | 问题按类型三档处理；「Why this company」按岗位起草并过校验 | `agent answers <job_id>` |
| 投递追踪 | 事件溯源的状态机、下一步建议、确认邮件告警、TSV 导出 | `agent board` |
| 邮件处理 | 只读 IMAP 拉取、分类、匹配投递、按误判代价分级写入或进人工队列 | `agent mail sweep` |
| 面试准备 | 汇总 JD 要点、技能差距、投出去的那版简历、内推人 | `agent prep <application_id>` |
| Agent loop | 模型按任务自行调用工具，全程轨迹留痕 | `agent run "<任务>"` |
| 自动化 | 具名定时任务；可选 OpenClaw 外壳提供常驻运行与手机查询 | `agent schedules` |

---

## 设计原则

1. **安全边界是工具注册表，不是提示词。** 提交申请、发邮件、点链接这些能力在代码里根本不存在，模型即使被 JD 或邮件里的注入内容说服也调不出来。`agent tools` 会把这份「刻意不存在」的清单一起打印。
2. **分层调用。** JD 全文、邮件正文这类大块不可信文本只在工具内部用独立上下文处理，不进 agent 的对话历史——同时控制成本和注入面。
3. **确定性校验。** 防幻觉、申请表问题分档、简历一页约束都是确定性代码，不是第二次模型调用。
4. **人在回路。** 简历审核、面试邀请确认、申请提交由人完成，并且这些能力不对 agent 开放。
5. **数据本地。** 数据库、母简历、邮箱凭据都不离开本机。

---

## 快速开始

### 1. 安装

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Windows 以外的系统把 `./.venv/Scripts/` 换成 `./.venv/bin/`。下文命令统一写作 `agent`；未激活虚拟环境时用 `./.venv/Scripts/agent.exe`。

可选组件：

```bash
./.venv/Scripts/python.exe -m pip install playwright
./.venv/Scripts/python.exe -m playwright install chromium
```

简历 PDF 渲染需要上面这两条；没装时 `agent tailor` 只产出 HTML，并会告诉你缺什么。用 OpenClaw 外壳还需要 `pip install -e ".[openclaw]"`。

### 2. 初始化

```bash
cp .env.example .env
agent init
```

`init` 建库并从 `config/*.example.yaml` 生成 `config/*.yaml`。它是幂等的，可以随时重跑，**已存在的配置文件不会被覆盖**。

### 3. 填内容

按顺序做这四件事，它们决定整个系统的产出质量：

1. **母简历** `config/master_profile.yaml`：所有能写的经历、项目、技能、申请表常见问题的标准答案、面试用的 STAR 故事。每条 bullet 一个全局唯一 id——简历定制时模型只输出 id，正文逐字从这里取。改完校验一次：

   ```bash
   agent profile check
   ```

2. **目标画像** `config/target_profile.yaml`：职位关键词、地点、职级、排除项。排除项比包含项更重要。

3. **目标公司** `config/companies.yaml`：用工具反查 ATS 类型和 board token，不要手工猜。

   ```bash
   agent resolve-ats "https://www.databricks.com/company/careers/open-positions" --name Databricks
   agent companies sync
   ```

   `email_domains` 填公司自己的域名，不要填 `greenhouse-mail.io` 这类多家共用的 ATS 发信域名。

4. **内推线索**：把每家目标公司认识的人写进 `contacts` 表。内推的面试转化率显著高于冷投。

### 4. 日常流程

```bash
agent fetch --notify                                  # 抓新岗位，推送到 Discord
agent jobs list --tier tier1_ai_engineer              # 看抓到什么
agent analyze                                         # 分析 JD、打分分档
agent tailor 267                                      # 定制简历
agent resume approve 8                                # 人工审核门
agent answers 267 --question "Why do you want to join us?"
agent applied 267 --via referral --resume-version 8   # 你自己提交后登记
agent mail sweep && agent mail queue                  # 邮件处理与人工确认
agent board                                           # 追踪表和下一步
```

---

## 配置

### 环境变量（`.env`）

| 变量 | 用途 | 何时需要 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 所有模型调用 | JD 分析起 |
| `DISCORD_WEBHOOK_URL` | 推送岗位和邮件提醒的频道 webhook | 需要推送时 |
| `DISCORD_USER_ID` | 聊天入口的白名单用户 | 用 OpenClaw 外壳时 |
| `IMAP_HOST` / `IMAP_PORT` / `IMAP_USER` / `IMAP_APP_PASSWORD` / `IMAP_MAILBOX` | 只读收信（Gmail 用应用专用密码，需先开两步验证） | 邮件处理 |
| `JHA_DB_PATH` | 数据库位置，默认 `data/jha.db` | 可选 |
| `JHA_DAILY_APPLY_LIMIT` | 每日投递上限，默认 8 | 可选 |
| `JHA_GHOST_AFTER_DAYS` | 多久没动静判为 ghosted，默认 30 | 可选 |

webhook URL 本身就是密钥，不要外传，也不要提交。

### 配置文件（`config/`）

| 文件 | 内容 |
|---|---|
| `master_profile.yaml` | 母简历：经历、项目、技能、`qa_bank`、`story_bank` |
| `target_profile.yaml` | 目标画像：职位关键词、地点、职级、排除项、优先分析的关键词 |
| `companies.yaml` | 目标公司：ATS 类型、board token、邮件域名、优先级、`why_note` |
| `schedules.yaml` | 定时任务：提示词、工具集、预算、调度设置 |

每个文件都有对应的 `*.example.yaml` 作为模板和字段说明。

---

## 命令参考

**配置与运维**

| 命令 | 说明 |
|---|---|
| `agent init` | 建库、补表、生成配置（幂等） |
| `agent profile check` | 校验母简历和目标画像：id 唯一性、引用完整性、未替换的占位符 |
| `agent resolve-ats <url\|名字>` | 反查 ATS 类型和 board token，打真实接口验证 |
| `agent companies sync` | `companies.yaml` 同步进数据库 |
| `agent stats` | 各表行数、投递状态分布 |
| `agent status rebuild` | 按事件全量重算状态缓存 |
| `agent export` | 导出追踪表 TSV |

**岗位**

| 命令 | 说明 |
|---|---|
| `agent fetch` | 抓取新岗位。`--explain` 打印初筛丢弃原因、`--dry-run` 不写库、`--no-detail` 跳过 JD 全文、`--notify` 推送 |
| `agent jobs list` | 列出岗位，按档位排序 |
| `agent jobs show <id>` | 单个岗位详情与 JD 全文 |
| `agent analyze` | 跑 JD 分析（工具内部调用，不经过 agent loop） |

**简历与申请**

| 命令 | 说明 |
|---|---|
| `agent tailor <job_id>` | 定制简历：选材 + 按 JD 关键词改写 + 渲染 + 校验。`--no-rewrite` 只选材、`--max-pages` 页数上限 |
| `agent resume list` / `resume approve <id>` | 简历版本与人工审核门 |
| `agent answers <job_id>` | 申请表问题起草。`--question` 可重复、`--questions <文件>` 每行一题、`--no-draft` 只套模板 |
| `agent prep <application_id>` | 面试准备材料 |

**投递与邮件**

| 命令 | 说明 |
|---|---|
| `agent applied <job_id>` | 登记一次你已手动提交的投递 |
| `agent confirm <id>` | 记下确认邮件到了 |
| `agent board` | 追踪表、下一步建议、确认邮件告警 |
| `agent mail sweep` | 拉取并处理新邮件（只读）。`--push-alerts` 推送需要处理的邮件 |
| `agent mail queue` / `accept <id>` / `dismiss <id>` | 人工确认队列 |

**Agent 与自动化**

| 命令 | 说明 |
|---|---|
| `agent run "<任务>"` | 跑 agent loop。`--read-only` 只给只读工具、`--schedule <名字>` 按定时任务定义跑 |
| `agent tools` | 工具清单、权限档，以及刻意不存在的能力 |
| `agent runs` | agent 干过什么；`--show <id>` 展开完整轨迹 |
| `agent spend` | 按用途和任务拆 LLM 成本，含权限毕业计数 |
| `agent schedules` | 列出定时任务定义 |
| `agent openclaw config` / `verify <path>` | 生成外壳配置；检查配置有没有被改松 |

---

## 自动化运行

定时任务定义在 `config/schedules.yaml`，每个任务包含提示词、工具集和预算——**行为落在可版本化的文件里，不写在 cron 命令行里**。仓库自带四个：`daily-jobs`、`email-sweep`、`weekly-review`、`phone-query`。

```bash
agent run --schedule daily-jobs
```

时间敏感的检测不经过模型，直接用确定性命令，适合交给系统调度器：

```bash
agent mail sweep --push-alerts                # 有面试邀请等需要处理的邮件就推送
agent fetch --analyze 30 --push-recommended   # 抓取、排队分析、推送新的推荐投递
```

超过 6 小时没有成功的邮件检测会被报为「已过期」——检测静默停掉时，你看到的只会是「最近没有面试邀请」。

**可选：OpenClaw 外壳。** 提供常驻运行和手机上的只读查询入口，不是新的安全边界。`agent openclaw config` 从 `schedules.yaml` 生成配置片段和 cron 命令，`agent openclaw verify` 检查配置是否被改松（沙箱、工具白名单、gateway 绑定、私信来源等，任何一项不满足都会失败）。部署步骤见路线图 §3.7。

---

## 安全模型

### 权限分档

| 档 | 含义 | 例子 |
|---|---|---|
| `READ` | 随便调 | 列岗位、查分析、看追踪表 |
| `WRITE` | 写本地库，允许自动执行（事件追加式，可纠错） | 登记投递、追加事件 |
| `GATED` | 对外动作，必须单次批准，不延续到下次 | 推送通知 |

### 刻意不存在的能力

提交申请、发送邮件、打开链接、删除记录——这些工具没有实现，测试里有断言守着它们不会被加进来。审核门（`agent resume approve`）和面试邀请确认（`agent mail accept`）只在命令行里，agent 调不到。

### 邮件只读

用 `EXAMINE` 打开邮箱、只用 `BODY.PEEK[]` 取信（不会把邮件标成已读），代码守卫用白名单只放行 SEARCH 和 FETCH。邮件正文和主题都不进 agent 上下文。

分类结果按误判代价处理：确认邮件和拒信自动写入；**面试邀请、OA、offer 永远进人工队列**。

### 简历防幻觉

三道结构性保证，不依赖提示词：

1. 选材 schema 里没有文本字段，模型只能输出 bullet id；
2. 渲染器只认 id，正文逐字取自母简历；
3. 成品文本再过一次校验，出现母简历以外的数字或专名就拒绝。

按 JD 关键词改写措辞时，每条改写单独过确定性校验（不新增或改动数字、不出现母简历以外的专名、新关键词必须在技能清单里、长度不明显增加），没过就退回原文。一页约束是渲染后数 PDF 页数、按超出比例裁剪得到的，不是提示词要求的。

### 提示词注入

JD、邮件、公司来源都是任何人都能写的文本。防线按强度排序：

1. 读原始文本的是工具内部的模型调用，它手里一个工具都没有；
2. 危险能力在工具注册表里不存在；
3. 工具结果在提示词里标记为不可信数据。

顺序很重要——第 1、2 条才是主力，提示词里的声明只是补充。规划中的自动提交会再加一层输出校验和目的地白名单，见路线图 §3.8。

---

## 成本控制

- **分层**：编排用 Sonnet，批量的 JD 分析和邮件分类用 Haiku。
- **大块文本不进 agent 上下文**：agent loop 每轮重发完整历史，让 agent 逐条读 JD 的成本是复利的。
- **硬预算**：`Budget` 限制单次 run 的调用次数和 token，超了就停并汇报已完成的部分。
- **按用途记账**：每次调用写 `llm_calls` 表，`agent spend` 按用途和定时任务拆账。

目标区间是每月 $10–40。

---

## 开发

```bash
./.venv/Scripts/python.exe -m pytest -q
```

642 个测试，全部离线（其中 7 个默认跳过，包括打真实 ATS 接口的 `--live` 冒烟测试）。测试固件是从真实接口抓的样本，保证确定性。

重点覆盖：状态机每条边、事件表的 append-only 触发器、母简历 id 完整性、ATS token 提取、工具边界、邮件只读、简历校验器、外壳配置的每一种改松方式。另有一组 agent 行为 eval（`tests/test_evals.py`），守单元测试测不到的东西：上下文有没有爆炸、被要求做越界的事时会不会拒绝。

约定：

- 所有文件 I/O 走 `config.read_text` / `config.write_text` 强制 utf-8（Windows 默认编码会在 JD 文本上炸）；
- `events` 表只能追加，UPDATE 和 DELETE 会被数据库触发器拒绝，更正靠追加 `status_override` 事件；
- 状态推导是纯函数，不碰数据库也不读时钟，状态机每条边都能钉死在测试里。

---

## 项目结构

```
config/                 *.example.yaml 进 git，同名 *.yaml 是你的真实内容
src/jha/
  cli.py                命令行入口
  db.py, schema.sql     SQLite 库与迁移
  config.py             路径、.env、强制 utf-8 的文件 I/O
  profile.py            母简历加载与校验
  sources/              Greenhouse / Lever / Ashby 适配器
  filters.py            规则初筛
  ingest.py             抓取管线、下架检测
  analyze.py            JD 分析（工具内部调用）
  tailor.py             简历选材与改写
  render.py             HTML 模板 → PDF，一页约束循环
  verify.py             确定性幻觉校验器
  questions.py          申请表问题三档分类
  why.py                「为什么想来」按岗位起草
  tracking.py           投递追踪、确认告警、导出
  status.py             事件 → 状态的纯函数推导
  mail/                 只读 IMAP、预过滤、分类、匹配、分级策略
  prep.py               面试准备材料
  notify.py             Discord 推送
  schedules.py          定时任务定义
  agent/                tools.py（注册表=安全边界）、loop.py、client.py、persistence.py
  mcp_server.py         外壳：按任务收窄的 MCP 服务器
  openclaw.py           外壳：配置生成与检查
  tools/resolve_ats.py  ATS 类型与 board token 反查
tests/                  离线测试与固件
data/                   SQLite 库、生成的简历（不进 git）
```

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 0 | 数据模型、母简历、校验器 | 完成 |
| Phase 1 | 岗位监控 | 完成 |
| Phase 2 | JD 分析与匹配 | 完成 |
| Phase 3 | 简历定制 | 完成 |
| Phase 4 | 投递追踪、申请表问题 | 完成 |
| Phase 4.5 | 表单预填 | 未做，不在关键路径 |
| Phase 5 | 邮件处理与状态更新 | 完成 |
| Phase 6 | 面试模拟 | 未实现 |
| Phase 7 | 打磨与运营 | 持续 |
| 外壳 | OpenClaw 常驻运行 | 代码完成，待实机验证 |
| §3.8 | 自动闭环：提交执行器、自动扩充公司列表 | 规划中 |

完整设计、关键决策记录和风险清单见 [job_hunting_agent_roadmap.md](job_hunting_agent_roadmap.md)。

---

## 免责声明

个人自用项目，暂未附带开源许可证。

- 只使用各 ATS 的公开官方接口，不抓取 LinkedIn / Indeed / Glassdoor，不做 Easy Apply。
- 申请提交由人完成。使用前请自行确认目标网站的使用条款。
- 简历内容由人审核后才能投出；工作授权、EEO 等法律相关问题只使用你亲自写好的答案或留空。
