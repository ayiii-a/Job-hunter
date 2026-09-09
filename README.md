# Job Hunting Agent

个人使用、人在回路的求职 agent。完整设计见 [job_hunting_agent_roadmap.md](job_hunting_agent_roadmap.md)。

> **只有模板进 git。** 母简历里会有你的真实姓名、电话、住址和完整履历，
> 所以 `config/*.yaml`、`.env`、`data/` 全部在 `.gitignore` 里，仓库中只保留 `*.example.yaml`。
> 代价是简历没有 git 版本历史——要的话自己另外备份（Phase 7 会加每日备份）。

当前进度：**Phase 0（骨架）完成**。抓取、分析、简历定制、邮件、面试模拟尚未实现。

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

**`email_domains` 一定要填。** Phase 5 把邮件匹配回投递记录的第一步就是查发件域名，空着的话那家公司的邮件会全部落到人工队列。

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
  tools/    resolve_ats.py
tests/      85 个测试
data/       SQLite 库（gitignored；Phase 7 会加每日备份）
```

---

## 下一步

Phase 6（面试模拟）和 Phase 0 是并行的，**不用等流水线建完**——有 JD 和你的经历就能开始练。路线图把它排在最前面是有理由的：模拟面试会暴露「哪条 bullet 你讲不清楚」，而那正是写母简历时最需要的反馈。

母简历成型之后再进 Phase 1（岗位监控）。
