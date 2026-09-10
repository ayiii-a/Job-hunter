# Job Hunting Agent

个人使用、人在回路的求职 agent。完整设计见 [job_hunting_agent_roadmap.md](job_hunting_agent_roadmap.md)。

> **只有模板进 git。** 母简历里会有你的真实姓名、电话、住址和完整履历，
> 所以 `config/*.yaml`、`.env`、`data/` 全部在 `.gitignore` 里，仓库中只保留 `*.example.yaml`。
> 代价是简历没有 git 版本历史——要的话自己另外备份（Phase 7 会加每日备份）。

当前进度：**Phase 0 · 1 · 2 · 3 · 4 · 5 + Agent loop 完成**。剩 Phase 6（面试模拟）。
面试模拟尚未实现。

---

## 装

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

```bash
./.venv/Scripts/agent.exe init
```

`init` 做两件事：建库（10 张表），以及从 `config/*.example.yaml` 生成你自己的 `config/*.yaml`。

它是幂等的，随时可以重跑——**已存在的配置文件绝不会被覆盖**。母简历是你写了两周的东西，不能被一条 init 冲掉。

---

## Phase 0 要你做的事

骨架已经就位，剩下的是**只有你能填的内容**。按这个顺序走：

### 1. 写母简历（1–2 周的晚上，别压缩）

编辑 `config/master_profile.yaml`（`init` 已经从模板生成好了）。里面的示例内容展示了结构，全部替换成你自己的。

改完随时校验：

```bash
./.venv/Scripts/agent.exe profile check
```

模板里的示例内容结构上是合法的（好让你先跑通再填），所以校验器会单独提醒你哪些占位符还没换掉。

这个校验器守的是 Phase 3 防幻觉设计的前提：**bullet ID 全局唯一、技能引用可解析、story 关联的 bullet 真实存在**。ID 一旦重复或悬空，后面 LLM 选出来的内容会静默错位——那是最难发现的一类错误，所以宁可现在啰嗦。

> 路线图第 7 节第 1 条：母简历投入的时间决定整个项目的上限。agent 只能重组你给它的材料。

### 2. 写目标画像

编辑 `config/target_profile.yaml`。**排除项比包含项更重要**——实测单是 Databricks 一家就有 870 个在招岗位、178 个不同地点，只有约三分之一在美国。

### 3. 列目标公司 + 查 board_token

别手工猜 token。用这个：

```bash
./.venv/Scripts/agent.exe resolve-ats "https://www.databricks.com/company/careers/open-positions" --name Databricks
```

它会抓页面、正则捞候选、**打真实 API 验证**，然后输出可以直接粘进 `config/companies.yaml` 的片段。只给公司名也行（靠猜 + 验证）。

填好后同步进库：

```bash
./.venv/Scripts/agent.exe companies sync
```

**`email_domains` 填公司自己的域名**（`ramp.com`），**不要**填 `greenhouse-mail.io` / `ashbyhq.com` 这类 ATS 共享发信域名——那是所有公司共用的，填进来等于「每一封 Greenhouse 邮件都是这家公司的」。代码会忽略它们，ATS 发来的信靠发件人显示名和主题认公司。

### 4. 填内推线索

`contacts` 表现在是空的。每家目标公司你认识谁、关系多强、上次联系是什么时候。

路线图把这张表叫作「全项目回报最高的一张表」——内推的面试转化率显著高于冷投，而这份工程里所有其他模块加起来，对结果的影响可能都不如在目标公司找到一个愿意推你的人。

---

## 命令

| 命令 | 作用 |
|---|---|
| `agent init` | 建库 / 补表（幂等） |
| `agent profile check` | 校验母简历和目标画像 |
| `agent resolve-ats <url\|名字>` | 反查 ATS 类型和 board_token，打真实接口验证 |
| `agent companies sync` | `companies.yaml` → 数据库 |
| `agent status rebuild` | 按 events 全量重算 status 缓存 |
| `agent stats` | 各表行数 + 投递状态分布 |
| `agent fetch` | 抓取目标公司的新岗位（Phase 1） |
| `agent jobs list` | 列出抓到的岗位，按档位排序 |
| `agent jobs show <id>` | 看单个岗位和 JD 全文 |
| `agent run "<任务>"` | 跑 agent loop，模型自己决定调哪些工具 |
| `agent tools` | 列出可用工具、权限档，以及**刻意不存在**的工具 |
| `agent spend` | 按用途和任务拆 LLM 成本，含权限毕业计数 |
| `agent analyze` | 直接跑 JD 分析（第二层，不经过 agent loop） |
| `agent runs` | 看 agent 干过什么；`--show <id>` 展开完整轨迹 |
| `agent tailor <job_id>` | 为某个岗位定制简历（选材 + 渲染 + 幻觉校验） |
| `agent resume list` | 列出简历版本；`approve <id>` 过审核门 |
| `agent schedules` | 列出定时任务；`run --schedule <名字>` 跑一个 |
| `agent applied <job_id>` | 记录一次**你已手动投完**的投递 |
| `agent board` | 追踪表 + 下一步建议 + 确认邮件告警 |
| `agent confirm <id>` | 记下确认邮件到了 |
| `agent export` | 导出 TSV，可直接粘进 Google Sheet |
| `agent answers <job_id>` | 申请表自定义问题起草 |
| `agent mail sweep` | 拉取并处理新邮件（只读） |
| `agent mail queue` | 人工确认队列；`accept` / `dismiss` / `show <id>` |
| `agent prep <application_id>` | 面试准备材料 |

`agent fetch` 的开关：`--explain` 显示初筛丢弃原因和样本（调过滤条件全靠它）、
`--dry-run` 只跑不写库、`--no-detail` 跳过 JD 全文抓取、`--notify` 推到 Telegram。

**定时抓取**（路线图建议每天 2–4 次，岗位发布 48 小时内投递回复率明显更高）：

```bash
cd "C:/Projects/Job Hunting Agent" && ./.venv/Scripts/agent.exe fetch --notify
```

---

## Phase 1：岗位监控

### 三个适配器是不对称的

路线图特别强调不能用一个 `fetch()` 糊过去，实测确认了这一点：

| | JD 全文 | 增量字段 | 时间戳 | 薪资 |
|---|---|---|---|---|
| **Greenhouse** | 要第二次调用 | **有 `updated_at`** | ISO8601 | 无 |
| **Lever** | 列表里就带 | 无 | **epoch 毫秒** | 无 |
| **Ashby** | 列表里就带 | 无 | ISO8601 | **结构化，不用过 LLM** |

差异写成了 `Adapter` 的两个类属性（`provides_jd_in_list` / `supports_incremental`），
ingest 据此决定抓取策略，而不是对三家做同样的事。

### 省钱的关键是顺序

**规则初筛跑在详情抓取之前。** 实测 Databricks 板子 869 个岗位，
过完地点和标题过滤只剩 7 个——详情请求就是 7 次而不是 869 次。
这比列表本身 745KB→9.5MB 的 12 倍差距更值钱。

Greenhouse 的 `updated_at` 再省一层：第二次抓取时详情请求降到 **0**。

### 调过滤条件

被初筛丢掉的岗位**不入库**（不然 5,000+ 条噪音会淹掉数据库），
所以要用 `--explain` 当场看：

```bash
./.venv/Scripts/agent.exe fetch --explain
```

它会打印丢弃原因的分布和样本。第一次跑就靠它抓到了三个真问题：

- **纽约的岗位被 Canada 排掉了** —— 排除检查跑在所有地点拼成的整串上，
  而 Ramp 的纽约岗位同时提供 Remote (Canada)。改成逐个地点单独判之后，
  初筛留下的岗位从 6 个变成 36 个。
- **`New York, NY (HQ)` 匹配不上 `United States`** —— 大部分美国岗位被静默漏掉。
  现在用 `preset:us`（州全名 + 逗号锚定的州缩写）。
- **`Sr Software Engineer`（无点）拦不住** —— 七个高级岗位混进了应届列表。

这三个都是**假阴性**：被误杀的岗位不入库，不看 `--explain` 永远不会发现。
路线图说的「前两周每天看漏报和误报」就是这个意思。

---

## Agent loop

模型决定**做什么**，Phase 0/1 的确定性代码决定**怎么做**。工具体内就是已经
测过的那些函数——抓取、初筛、状态推导。模型不重新实现它们，只调用它们，
所以跑偏的上界是「调错了工具」，而不是「算错了结果」。

```bash
./.venv/Scripts/agent.exe run "看看今天有什么新岗位，有内推路径的排前面"
./.venv/Scripts/agent.exe run --read-only "巡检：抓取器有没有静默失败"
```

### 安全边界是工具注册表，不是 prompt

路线图那几条原则（提交由人点、邮件只读、不点链接）现在靠**不存在对应的工具**
来保证。模型可能被 JD 或邮件正文里的注入内容诱导，但它调不出不存在的函数。

跑 `agent tools` 会把这份「刻意不存在」的清单一起打印出来——它是安全边界，
不是待办事项。**加新工具之前先问：这个能力被滥用的最坏后果是什么。**

三档权限：`READ` 随便调；`WRITE` 写本地库、允许自动执行（因为 events
追加式可纠错）；`GATED` 外发动作，必须**单次**批准，不延续到下次。

### 分层：重活不在 agent 上下文里做

agent loop 每轮重发完整对话历史，所以让 agent 逐条读 JD 的成本是**复利**的——
实测读 20 条是流水线的 8.3 倍，50 条 19.7 倍（$1.05 vs $0.13）。

所以 JD 全文不进 agent 上下文：`analyze_jobs` 在**工具内部**逐条调 Haiku、
独立上下文、只把 `{job_id, verdict, top_gaps}` 返回。`list_jobs` 根本不返回 JD，
`get_job` 默认也不给，要原文得显式 `include_jd=true`。

**分层顺带解决了注入**：读 JD 全文的第二层模型手里一个工具都没有——
JD 里藏的指令说服了它也无处可施。

### 防幻觉：三道结构性保证

定制简历是这个项目最容易翻车的地方——一次编造被面试官问出来就全盘皆输。
所以保证不写在 prompt 里，写在结构里：

1. **选材 schema 里没有文本字段**，模型只能给 bullet id
2. **渲染器只认 id**，从母简历逐字取原文——模型给的字符串进不了成品
3. **最终闸门查产出物**，出现母简历以外的数字或专名就拒

第 2 道最硬：前两道都被绕过，渲染器也拿不出母简历里没有的句子。
校验器（`src/jha/verify.py`）全部是确定性代码，不是第二次 LLM 调用——
**安全阀不该建在会随机放水的组件上。**

一页约束同理，是**量出来的**不是 prompt 说的：渲染 → 数 PDF 页数 →
按超出比例砍（先砍没数字的）→ 重渲。砍无可砍时如实报页数，不悄悄截断。

```bash
./.venv/Scripts/agent.exe tailor 12
```

生成的版本**未经审核**。`approve_resume` 刻意不是 agent 的工具——
审核门如果 agent 能自己过，那就不是门。而且它有牙齿：
把未审核的版本绑到投递记录上会直接报错。

```bash
./.venv/Scripts/agent.exe resume approve 1
```

### 轨迹留痕

每次 run 写 `agent_runs` / `agent_steps`。无人值守跑却查不到它干了什么，
这个组合不能上线。

```bash
./.venv/Scripts/agent.exe runs --show 3
```

同一份数据支撑三件事：复盘（那条状态为什么被改了）、按任务拆账
（哪个定时任务在烧钱）、以及**权限毕业计数**——某个 GATED 工具被批准过
多少次而没出事，决定它能不能降到 WRITE。

### 投递追踪

```bash
./.venv/Scripts/agent.exe applied 14 --via referral --referral Wei --resume-version 1
./.venv/Scripts/agent.exe board
```

`board` 会算出每条投递的**下一步**（纯规则，不是模型猜的），并告警
**投出去超过 24 小时还没收到确认邮件**的——确认邮件是「申请真的进系统了」
的唯一地面真相，没有它就可能是白投。

**每日上限 agent 不能突破，人可以**（`--force`）。上限的用途不是省力，
是逼你投得准：一天 8 家才有时间给每家写像样的「Why this company」、查内推。

### 申请表问题：三档处理

```bash
./.venv/Scripts/agent.exe answers 14 --question "Will you require sponsorship?"
```

| 档 | 处理 |
|---|---|
| 工作授权 / 签证 / 薪资 | **照抄 qa_bank 原文**，一个字不改，永不过 LLM |
| EEO 自愿披露、犯罪记录 | **一个字都不填**，连 qa_bank 都不查 |
| 其余 | 覆盖了就套模板标待审；没覆盖就留空，**绝不凭空起草** |

分档是确定性正则——判断「这是不是 EEO 问题」不该交给一个可能判错的组件。

### 邮件：只读、分级、人工确认

先在 `.env` 填 `IMAP_USER` 和 `IMAP_APP_PASSWORD`
（Gmail：开两步验证 → Google 账号 → 安全性 → 应用专用密码）。

```bash
./.venv/Scripts/agent.exe mail sweep
./.venv/Scripts/agent.exe mail queue
```

**只读是三层保证**：`EXAMINE` 打开邮箱；只用 `BODY.PEEK[]` 取信（普通 `BODY[]` 会把邮件标成已读）；
代码守卫用白名单，`uid()` 只放行 SEARCH / FETCH。

**按误判代价分级**：确认邮件和拒信自动写入（events 可追加纠错）；面试邀请、OA、offer **永远进人工队列**。
判成拒信但正文里有排期语言（availability / calendly / next round）的也进队列——
把面试邀请当拒信是邮件模块里唯一不可挽回的错误。

```bash
./.venv/Scripts/agent.exe mail accept 12
./.venv/Scripts/agent.exe prep 3
```

`accept` 刻意不是 agent 的工具。邮件正文——连主题行——都不进 agent 的上下文。

面试邀请想即时推到 Telegram：把 `config/schedules.yaml` 里 `email-sweep` 的 `allow_notify` 改成 true。不改的话推送停在「待批准」，下次看 `agent runs` 时还在。

### 定时任务：行为住在文件里

`config/schedules.yaml` 定义具名任务——提示词 + 工具集 + 预算：

```bash
./.venv/Scripts/agent.exe run --schedule daily-jobs
```

**为什么不直接在 cron 命令行里写提示词**：提示词是会改变 agent 行为的东西，
写在命令行里改一次就没有历史，你没法 diff、没法回滚。落成文件之后它和
`target_profile.yaml` 一样是可版本化的工件。

**按工具名收窄的主要收益是爆炸半径，不是省 token**（实测每轮省约 1,500 tokens，
月省 $2 出头）。`daily-jobs` 只拿到 7 个工具，`record_application` /
`tailor_resume` / `append_event` 在结构上够不着——哪怕它被 JD 里的注入内容说服了。

工具名写错会直接报错，不会静默少给一个。

### 预算是硬的

循环次数由模型决定，所以上限必须由代码给。`Budget` 限制 LLM 调用次数和
token 上限，超了就停并汇报已完成的部分。每次调用写 `llm_calls` 表，
`agent spend` 按用途拆账。

---

## 骨架里的两个关键设计

这两条是路线图的架构原则，Phase 0 把它们落成了**代码强制**而不是口头约定。

### events 是真相源，`applications.status` 只是缓存

`events` 表上挂了触发器，**UPDATE 和 DELETE 都会被数据库直接拒绝**：

```
sqlite3.IntegrityError: events 是追加式日志：不允许 UPDATE。要更正请追加 status_override 事件
```

状态由 [`status.py::derive_status`](src/jha/status.py) 从事件推导。它是纯函数——不碰数据库、不读时钟（`now` 由调用方传入），所以状态机的每条边都能钉死在测试里。

这个设计的实际价值在 Phase 5：邮件分类误判之后不用改历史，追加一条 `status_override` 事件再 `agent status rebuild` 就行。**正因为误判可纠正，拒信的自动分类才不必过度保守。**

### 所有文件 I/O 强制 utf-8

Windows 上 Python 默认编码是 cp1252，而 JD 文本里满是非 ASCII（实测抓到过日文岗位标题、smart quotes、em-dash）。裸 `open()` 迟早炸，而且崩的位置离真正的原因很远。

所以读写一律走 [`config.read_text` / `config.write_text`](src/jha/config.py)，CLI 入口调 `force_utf8_stdio()` 把 stdout 也钉死。

---

## 测试

```bash
./.venv/Scripts/python.exe -m pytest -q
```

85 个测试，全部离线。重点覆盖状态推导（状态机每条边 + ghosted 规则 + 人工更正通道）、append-only 触发器、母简历 ID 完整性、以及 ATS token 提取。

---

## 目录

```
config/     *.example.yaml 进 git；同名的 *.yaml 是你的真实内容，不进 git
src/jha/    schema.sql, db.py, status.py, profile.py, cli.py
  agent/    tools.py（注册表=安全边界）, loop.py（主循环）
            client.py（记账+预算+第二层调用）, persistence.py（轨迹留痕）
  analyze.py  Phase 2 第二层分析器（不在 agent loop 里）
  tailor.py   Phase 3 选材（只输出 bullet id）
  tracking.py Phase 4 追踪表、确认告警、下一步、导出
  questions.py Phase 4 申请表问题三档处理
  mail/       Phase 5 只读 IMAP、预过滤、分类、匹配、分级策略、确认队列
  prep.py     Phase 5 面试准备材料
  render.py   HTML 模板 → Playwright PDF + 一页约束循环
  verify.py   确定性幻觉校验器
  sources/  三个 ATS 适配器 + RawJob 归一化
  tools/    resolve_ats.py
            filters.py（规则初筛）, ingest.py（抓取管线）, notify.py
tests/      166 个离线测试 + 5 个 --live 冒烟测试
  fixtures/ 从真实接口抓的样本，保证测试离线且确定
data/       SQLite 库（gitignored；Phase 7 会加每日备份）
```

---

## 下一步

按路线图的顺序，接下来是 **Phase 2（JD 分析与匹配）**：给每个岗位出结构化分析 +
匹配档位（`strong_apply` / `apply` / `stretch` / `skip`）+ gap 列表。

但在那之前有两件更要紧的事：

1. **扩充 `config/companies.yaml`。** 现在只有 Databricks 和 Ramp 两个示例。
   路线图建议 30–80 家。用 `agent resolve-ats` 批量查 board_token。
2. **给母简历的 bullet 补数字。** 12/12 条没有量化结果，这会直接卡住 Phase 3 的选材。

Phase 6（面试模拟）对流水线零依赖，随时可以插进来——而且它能反过来帮你做第 2 件事：
模拟面试追问「这个数据怎么来的」，正好逼出那些缺失的数字。
