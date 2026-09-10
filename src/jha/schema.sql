-- Job Hunting Agent —— 数据库骨架
-- 对应路线图第 2.2 节。表结构刻意保持 Postgres 可迁：不用 SQLite 特有类型。
--
-- 关于数组字段：SQLite 没有数组类型，一律存 JSON TEXT（列名带 _json 后缀）。
-- 迁 Postgres 时换成 jsonb 或原生数组即可。

PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------------------
-- companies —— 目标公司清单。跨 Phase 的硬依赖：
--   Phase 1 靠 board_token 抓取；Phase 5 靠 email_domains 把邮件匹配回投递记录。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    id                INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL UNIQUE,
    ats_type          TEXT,             -- greenhouse | lever | ashby | workday | other | unknown
    board_token       TEXT,             -- ATS 上的板子标识；常常不等于公司名，用 resolve-ats 查
    careers_url       TEXT,
    email_domains_json TEXT NOT NULL DEFAULT '[]',   -- ["acme.com", "greenhouse-mail.io"]
    priority          INTEGER NOT NULL DEFAULT 3,    -- 1 最高
    notes             TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (ats_type, board_token)
);


-- ---------------------------------------------------------------------------
-- contacts —— 内推线索。路线图里回报最高的一张表：
--   任何岗位在建议「去投」之前，先查这里有没有人能推你。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contacts (
    id                INTEGER PRIMARY KEY,
    company_id        INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    name              TEXT    NOT NULL,
    relationship      TEXT,             -- 前同事 / 校友 / 朋友的朋友 / 会议认识 ...
    strength          INTEGER NOT NULL DEFAULT 2,    -- 1 弱 2 中 3 强（愿意主动帮你推）
    last_contacted_at TEXT,
    source            TEXT,
    notes             TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);


-- ---------------------------------------------------------------------------
-- jobs —— 抓到的岗位。JD 全文必存：岗位下线后就拿不回来了，但面试时还需要它。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY,
    company_id        INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    source            TEXT    NOT NULL,  -- greenhouse | lever | ashby | manual
    external_id       TEXT    NOT NULL,  -- 源站的岗位 id
    title             TEXT    NOT NULL,
    location          TEXT,
    remote_type       TEXT,
    url               TEXT,
    jd_text           TEXT,
    salary_raw        TEXT,              -- 能从 API 直接拿到就存原文（如 Ashby），不过 LLM
    posted_at         TEXT,
    first_seen_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    is_active         INTEGER NOT NULL DEFAULT 1,
    is_shortlisted    INTEGER NOT NULL DEFAULT 0,   -- 取代旧状态机里的 watching
    content_hash      TEXT,
    source_updated_at TEXT,              -- Greenhouse 有 updated_at，可省掉全文抓取
    miss_count        INTEGER NOT NULL DEFAULT 0,   -- 连续几次抓取没出现；到 2 判下架
    screen_tier       TEXT,              -- 规则初筛命中的档位（title_tiers）
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_company     ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_active      ON jobs(is_active);
CREATE INDEX IF NOT EXISTS idx_jobs_shortlisted ON jobs(is_shortlisted);


-- ---------------------------------------------------------------------------
-- job_analysis —— Phase 2 的产出。
--   verdict 是判定档位（strong_apply/apply/stretch/skip），match_score 只作参考。
--   scorer_version 必填：改了打分 prompt 就 bump，否则新旧分数不可比。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_analysis (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    required_skills_json TEXT NOT NULL DEFAULT '[]',
    nice_to_have_json   TEXT   NOT NULL DEFAULT '[]',
    seniority           TEXT,
    years_required      TEXT,
    visa_hint           TEXT,
    salary_range        TEXT,
    remote_policy       TEXT,
    red_flags_json      TEXT   NOT NULL DEFAULT '[]',
    jd_summary_plain    TEXT,
    verdict             TEXT,            -- strong_apply | apply | stretch | skip
    match_score         INTEGER,         -- 参考值，不做阈值开关
    gaps_json           TEXT   NOT NULL DEFAULT '[]',
    rationale           TEXT,
    scorer_version      TEXT   NOT NULL,
    analyzed_at         TEXT   NOT NULL DEFAULT (datetime('now')),
    UNIQUE (job_id, scorer_version)
);
CREATE INDEX IF NOT EXISTS idx_analysis_verdict ON job_analysis(verdict);


-- ---------------------------------------------------------------------------
-- resume_versions —— Phase 3 的产出。
--   generated_for_job_id 可空，语义是「为哪个岗位生成的」；
--   实际投给了哪个岗位，权威关联走 applications，避免两条 FK 路径互相矛盾。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resume_versions (
    id                   INTEGER PRIMARY KEY,
    generated_for_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    selected_bullet_ids_json TEXT NOT NULL DEFAULT '[]',
    rendered_pdf_path    TEXT,
    rendered_html_path   TEXT,
    page_count           INTEGER,        -- render-measure-retry 循环的结果
    diff_summary         TEXT,
    approved_at          TEXT,           -- 你确认之后才填；未确认的不许投出去
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);


-- ---------------------------------------------------------------------------
-- applications —— 真正投出去的才建行。
--   status 是【物化缓存】，不是真相源。真相是 events，见 status.py::derive_status。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS applications (
    id                   INTEGER PRIMARY KEY,
    job_id               INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    resume_version_id    INTEGER REFERENCES resume_versions(id) ON DELETE SET NULL,
    status               TEXT,           -- 缓存字段：由 derive_status(events) 重算
    applied_at           TEXT,
    applied_via          TEXT,           -- referral | company_site | ats_direct | ...
    referred_by_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    confirmation_seen_at TEXT,           -- 24h 内没值就告警：申请可能被静默丢弃
    notes                TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (job_id)
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);


-- ---------------------------------------------------------------------------
-- events —— 追加式日志，永不删改。这是整个系统的真相源。
--   下面的触发器把「永不删改」从口头约定变成数据库强制的约束。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    type           TEXT    NOT NULL,
    occurred_at    TEXT    NOT NULL,
    source         TEXT    NOT NULL,     -- email | manual | agent
    raw_ref        TEXT,                 -- 例如触发本事件的 emails.id
    payload_json   TEXT    NOT NULL DEFAULT '{}',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id, occurred_at);

-- 误判不靠改历史来修，靠追加一条 status_override 事件来修。
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events 是追加式日志：不允许 UPDATE。要更正请追加 status_override 事件');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events 是追加式日志：不允许 DELETE。要更正请追加 status_override 事件');
END;


-- ---------------------------------------------------------------------------
-- emails —— Phase 5。body_text 必存：
--   没有正文就没有那 50 封邮件的回归测试集，而邮件删了就拿不回来了。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS emails (
    id                    INTEGER PRIMARY KEY,
    uid                   TEXT    NOT NULL UNIQUE,   -- IMAP UID
    from_addr             TEXT,
    subject               TEXT,
    body_text             TEXT,
    received_at           TEXT,
    classification        TEXT,        -- rejection | oa_invite | interview_invite | ...
    confidence            REAL,
    matched_application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    reviewed              INTEGER NOT NULL DEFAULT 0,
    message_id            TEXT,
    from_domain           TEXT,
    role_hint             TEXT,
    summary               TEXT,
    dates_json            TEXT NOT NULL DEFAULT '[]',
    links_json            TEXT NOT NULL DEFAULT '[]',
    action_required       INTEGER,
    policy                TEXT,
    review_status         TEXT,
    reason                TEXT,
    event_id              INTEGER,
    classifier_version    TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_emails_class    ON emails(classification);
CREATE INDEX IF NOT EXISTS idx_emails_reviewed ON emails(reviewed);


-- ---------------------------------------------------------------------------
-- llm_calls —— Phase 7 的成本分析靠它。Phase 0 就建好，事后补埋点很烦。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY,
    called_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    purpose       TEXT    NOT NULL,      -- jd_analysis | resume_select | email_classify | ...
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    ref_type      TEXT,                  -- job | application | email | ...
    ref_id        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_purpose ON llm_calls(purpose, called_at);


-- ---------------------------------------------------------------------------
-- fetch_runs —— 每次抓取的结果。
--   抓取器静默失败是最危险的失败模式：你会以为「最近没什么新岗位」，
--   实际是适配器挂了两周。所以失败告警从 Phase 1 第一天就要有，不能等 Phase 7。
--   detail_fetches 同时也是成本可见性：它应该远小于 listed_count，
--   不然说明增量抓取没生效。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_runs (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    source         TEXT    NOT NULL,
    started_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    ok             INTEGER,
    listed_count   INTEGER NOT NULL DEFAULT 0,   -- 接口返回了多少个岗位
    kept_count     INTEGER NOT NULL DEFAULT 0,   -- 规则初筛之后剩多少
    new_count      INTEGER NOT NULL DEFAULT 0,
    updated_count  INTEGER NOT NULL DEFAULT 0,
    detail_fetches INTEGER NOT NULL DEFAULT 0,   -- 实际打了多少次详情请求
    deactivated    INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_company ON fetch_runs(company_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_ok      ON fetch_runs(ok, started_at);


-- ---------------------------------------------------------------------------
-- agent_runs / agent_steps —— agent 自己的追加式日志。
--
--   agent 之于自己，就像 events 之于投递。无人值守每天自动跑、还允许写库，
--   却查不到它到底干了什么，这个组合不能上线。
--
--   有了它才能做三件事：
--     复盘      那条状态为什么被改了 —— 翻 agent_steps
--     按任务拆账 哪个定时任务在烧钱 —— agent_runs.cost_usd group by schedule_name
--     权限毕业   某个 GATED 工具已经被批准过多少次而没出事（见 §0）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
    id             INTEGER PRIMARY KEY,
    task           TEXT    NOT NULL,
    schedule_name  TEXT,                          -- 定时任务名；手动跑则为 NULL
    started_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    ok             INTEGER,
    stopped_because TEXT,
    model          TEXT,
    llm_calls      INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL    NOT NULL DEFAULT 0,
    tool_calls     INTEGER NOT NULL DEFAULT 0,
    pending_approvals_json TEXT NOT NULL DEFAULT '[]',
    final_text     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started  ON agent_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_schedule ON agent_runs(schedule_name, started_at);

CREATE TABLE IF NOT EXISTS agent_steps (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    kind       TEXT    NOT NULL,      -- text | tool_use | tool_result | denied | error
    tool_name  TEXT,
    args_json  TEXT,
    result_summary TEXT,              -- 截断保存；全文在 jobs/emails 等原表里
    error      TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_steps_run  ON agent_steps(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_steps_tool ON agent_steps(tool_name, kind);


-- ---------------------------------------------------------------------------
-- schema 版本，为后面的迁移留口子
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_version (version) VALUES (4);
