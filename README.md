# Job Hunting Agent

A personal job-search pipeline: **job monitoring → JD analysis → resume tailoring → application-question drafting → application tracking → email processing**.

Human-in-the-loop by design: data stays on your machine and every outbound action is confirmed by a person. The model judges and drafts; deterministic code executes and verifies.

| | |
|---|---|
| Runtime | Python 3.11+ (Windows / macOS / Linux) |
| Storage | Local SQLite + local YAML config |
| Models | Anthropic Claude (Sonnet for orchestration, Haiku for batch work) |
| Status | Phases 0–5 and the agent loop are done; the OpenClaw shell is code-complete and awaiting a live trial; interview practice is not implemented |
| Language | CLI output, code comments and the design doc are in Chinese |
| Design doc | [job_hunting_agent_roadmap.md](job_hunting_agent_roadmap.md) |

> **Only templates are committed.** The master resume holds your real name, phone number, address and full work history, so `config/*.yaml`, `.env` and `data/` are all in `.gitignore`; the repo keeps only `*.example.yaml`. The trade-off is that your config has no git history — back it up yourself.

---

## Contents

- [Features](#features)
- [Design principles](#design-principles)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [Automation](#automation)
- [Security model](#security-model)
- [Cost control](#cost-control)
- [Development](#development)
- [Project structure](#project-structure)
- [Roadmap](#roadmap)
- [Disclaimer](#disclaimer)

---

## Features

| Module | What it does | Entry point |
|---|---|---|
| Job monitoring | Pulls postings from the official Greenhouse / Lever / Ashby APIs and screens them by location, title and seniority rules before storing | `agent fetch` |
| JD analysis | Assigns each posting a verdict (strong_apply / apply / stretch / skip) and extracts required skills, years of experience and gaps | `agent analyze` |
| Resume tailoring | Selects bullets from the master resume by id, rewrites wording toward JD keywords, renders a PDF, enforces one page and checks for fabrication | `agent tailor <job_id>` |
| Application questions | Handles form questions in three tiers; drafts "Why this company" answers per job and verifies them | `agent answers <job_id>` |
| Application tracking | Event-sourced state machine, next-step suggestions, missing-confirmation alerts, TSV export | `agent board` |
| Email processing | Read-only IMAP fetch; summarizes and classifies each message, updates the matching application or creates one for applications made elsewhere, and pushes anything that needs action | `agent mail sweep` |
| Interview prep | Collects JD highlights, skill gaps, the exact resume version you sent, and referral contacts | `agent prep <application_id>` |
| Agent loop | The model calls tools on its own to complete a task; every step is logged | `agent run "<task>"` |
| Automation | Named scheduled tasks; an optional OpenClaw shell adds always-on running and read-only phone queries | `agent schedules` |

---

## Design principles

1. **The security boundary is the tool registry, not the prompt.** Submitting applications, sending email and opening links simply do not exist in the code, so a model persuaded by injected text in a JD or email still cannot call them. `agent tools` prints this list of deliberately missing capabilities.
2. **Layered model calls.** Large untrusted text — full JDs, email bodies — is processed only inside tools, in isolated single-shot calls, and never enters the agent's conversation history. This controls both cost and the injection surface.
3. **Deterministic verification.** Fabrication checks, application-question tiering and the one-page constraint are deterministic code, not a second model call.
4. **Human in the loop.** Resume review, offer confirmation and application submission are done by a person, and none of these capabilities is exposed to the agent.
5. **Local data.** The database, master resume and mailbox credentials never leave your machine.

---

## Quick start

### 1. Install

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

On systems other than Windows, replace `./.venv/Scripts/` with `./.venv/bin/`. The commands below are written as `agent`; without an activated virtual environment, use `./.venv/Scripts/agent.exe`.

Optional components:

```bash
./.venv/Scripts/python.exe -m pip install playwright
./.venv/Scripts/python.exe -m playwright install chromium
```

These two are needed to render resume PDFs; without them `agent tailor` produces HTML only and tells you what is missing. The OpenClaw shell additionally needs `pip install -e ".[openclaw]"`.

### 2. Initialize

```bash
cp .env.example .env
agent init
```

`init` creates the database and generates `config/*.yaml` from `config/*.example.yaml`. It is idempotent and safe to re-run: **existing config files are never overwritten**.

### 3. Fill in your content

Do these four things in order — they determine the quality of everything downstream:

1. **Master resume** `config/master_profile.yaml`: every experience, project and skill you can write about, standard answers to common application questions, and STAR stories for interviews. Every bullet has a globally unique id — when tailoring, the model outputs only ids and the text is copied verbatim from here. Validate after each edit:

   ```bash
   agent profile check
   ```

2. **Target profile** `config/target_profile.yaml`: title keywords, locations, seniority and exclusions. Exclusions matter more than inclusions.

3. **Target companies** `config/companies.yaml`: look up each company's ATS type and board token with the resolver instead of guessing.

   ```bash
   agent resolve-ats "https://www.databricks.com/company/careers/open-positions" --name Databricks
   agent companies sync
   ```

   Set `email_domains` to the company's own domain, not a shared ATS sender domain such as `greenhouse-mail.io`.

4. **Referral contacts**: record the people you know at each target company in the `contacts` table. Referrals convert to interviews far better than cold applications.

### 4. Daily workflow

```bash
agent fetch --notify                                  # fetch new postings, push to Discord
agent jobs list --tier tier1_ai_engineer              # review what came in
agent analyze                                         # analyze and grade JDs
agent tailor 267                                      # tailor a resume
agent resume approve 8                                # human review gate
agent answers 267 --question "Why do you want to join us?"
agent applied 267 --via referral --resume-version 8   # record it after you submit
agent mail sweep && agent mail queue                  # process email, confirm the queue
agent board                                           # tracking board and next steps
```

---

## Configuration

### Environment variables (`.env`)

| Variable | Purpose | Needed for |
|---|---|---|
| `ANTHROPIC_API_KEY` | All model calls | JD analysis onward |
| `DISCORD_WEBHOOK_URL` | Channel webhook for job and email alerts | Push notifications |
| `DISCORD_USER_ID` | Allow-listed user for the chat entry point | The OpenClaw shell |
| `IMAP_HOST` / `IMAP_PORT` / `IMAP_USER` / `IMAP_APP_PASSWORD` / `IMAP_MAILBOX` | Read-only mail access (for Gmail, use an app password; 2-Step Verification must be on) | Email processing |
| `JHA_DB_PATH` | Database location, default `data/jha.db` | Optional |
| `JHA_DAILY_APPLY_LIMIT` | Daily application limit, default 8 | Optional |
| `JHA_GHOST_AFTER_DAYS` | Days of silence before an application is marked ghosted, default 30 | Optional |

The webhook URL is itself a secret: don't share it and don't commit it.

### Config files (`config/`)

| File | Contents |
|---|---|
| `master_profile.yaml` | Master resume: experiences, projects, skills, `qa_bank`, `story_bank` |
| `target_profile.yaml` | Target profile: title keywords, locations, seniority, exclusions, keywords to analyze first |
| `companies.yaml` | Target companies: ATS type, board token, email domains, priority, `why_note` |
| `schedules.yaml` | Scheduled tasks: prompt, tool set, budget, scheduling settings |

Each file has a matching `*.example.yaml` that serves as the template and field reference.

---

## Command reference

**Setup and maintenance**

| Command | Description |
|---|---|
| `agent init` | Create the database, add missing tables, generate config (idempotent) |
| `agent profile check` | Validate the master resume and target profile: id uniqueness, reference integrity, leftover placeholders |
| `agent resolve-ats <url\|name>` | Look up the ATS type and board token, verified against the live API |
| `agent companies sync` | Sync `companies.yaml` into the database |
| `agent stats` | Row counts per table and application status distribution |
| `agent status rebuild` | Recompute the status cache from events |
| `agent export` | Export the tracking board as TSV |

**Jobs**

| Command | Description |
|---|---|
| `agent fetch` | Fetch new postings. `--explain` shows why postings were screened out, `--dry-run` skips writes, `--no-detail` skips full JD fetches, `--notify` pushes results |
| `agent jobs list` | List postings, sorted by tier |
| `agent jobs show <id>` | One posting with its full JD |
| `agent analyze` | Run JD analysis (an in-tool call, outside the agent loop) |

**Resumes and applications**

| Command | Description |
|---|---|
| `agent tailor <job_id>` | Tailor a resume: selection + JD-keyword rewriting + rendering + verification. `--no-rewrite` selects only, `--max-pages` sets the page limit |
| `agent resume list` / `resume approve <id>` | Resume versions and the human review gate |
| `agent answers <job_id>` | Draft answers to application questions. `--question` is repeatable, `--questions <file>` takes one per line, `--no-draft` uses the template only |
| `agent prep <application_id>` | Interview prep pack |

**Applications and email**

| Command | Description |
|---|---|
| `agent applied <job_id>` | Record an application you submitted yourself |
| `agent confirm <id>` | Record that a confirmation email arrived |
| `agent board` | Tracking board, next-step suggestions, missing-confirmation alerts |
| `agent mail sweep` | Fetch and process new email (read-only). `--push-alerts` pushes messages that need action; `--requeue` re-runs messages still in the review queue under the current rules |
| `agent mail queue` / `accept <id>` / `dismiss <id>` | Human review queue. `accept --create` records an application that isn't in the table yet (`--company` / `--role` to override the names) |

**Agent and automation**

| Command | Description |
|---|---|
| `agent run "<task>"` | Run the agent loop. `--read-only` gives read tools only, `--schedule <name>` runs a scheduled-task definition |
| `agent tools` | Tool list, permission tiers, and deliberately missing capabilities |
| `agent runs` | What the agent did; `--show <id>` expands a full trace |
| `agent spend` | LLM cost by purpose and task, plus permission-graduation counts |
| `agent schedules` | List scheduled-task definitions |
| `agent openclaw config` / `verify <path>` | Generate shell config; check it hasn't been loosened |

---

## Automation

Scheduled tasks are defined in `config/schedules.yaml`; each one bundles a prompt, a tool set and a budget. **Behavior lives in a versionable file, not in a cron command line.** Four come with the repo: `daily-jobs`, `email-sweep`, `weekly-review` and `phone-query`.

```bash
agent run --schedule daily-jobs
```

Time-sensitive checks bypass the model entirely and run as deterministic commands, which suits a system scheduler:

```bash
agent mail sweep --push-alerts                # push when an interview invite or similar needs action
agent fetch --analyze 30 --push-recommended   # fetch, queue analysis, push new recommendations
```

If no mail check has succeeded for more than 6 hours, it is reported as stale — when detection silently stops, all you would otherwise see is "no interview invites lately".

**Optional: the OpenClaw shell.** It adds always-on running and a read-only query entry point from your phone; it is not a new security boundary. `agent openclaw config` generates config fragments and cron commands from `schedules.yaml`, and `agent openclaw verify` checks that the config hasn't been loosened (sandboxing, tool allow-lists, gateway binding, who may send DMs, and more — any failed check fails the command). Deployment steps are in §3.7 of the design doc.

---

## Security model

### Permission tiers

| Tier | Meaning | Examples |
|---|---|---|
| `READ` | Free to call | List jobs, read analyses, view the tracking board |
| `WRITE` | Writes the local database; may run automatically (events are append-only and correctable) | Record an application, append an event |
| `GATED` | Outbound action; requires one-time approval that does not carry over to the next run | Push a notification |

### Deliberately missing capabilities

Submitting applications, sending email, opening links and deleting records are not implemented, and tests assert they never get added. The review gate (`agent resume approve`) and review-queue confirmation (`agent mail accept`) exist only on the command line, out of the agent's reach.

### Read-only email

The mailbox is opened with `EXAMINE`, messages are fetched only with `BODY.PEEK[]` (so nothing gets marked as read), and a code-level allow-list permits only SEARCH and FETCH. Neither email bodies nor subject lines enter the agent's context.

Classification results are routed by the cost of getting them wrong. Confirmations, rejections, interview invites and OAs update the matching application automatically, and invites and OAs also trigger a push. **Offers always go to the human review queue**: they are few, costly to get wrong, and offer scams target new grads. A rejection whose body contains scheduling language is queued too, so an invite is never filed as a rejection.

Applications don't have to come from this tool. When an email matches no application — you applied on LinkedIn or a company site — a record is created, but only if the sender is trustworthy (a registered company domain, an ATS, or a job board) and the company name actually appears in the email. Company and role names taken from email pass deterministic checks (character set, length, present in the original text) before they are stored, because they later appear in pushes and in the tracking table the agent can read. Each application shows the latest email summary on `agent board` and in the export; the summary is never given to the agent.

### Resume fabrication checks

Three structural guarantees, none of which depends on the prompt:

1. The selection schema has no text field — the model can output only bullet ids;
2. The renderer accepts only ids and copies text verbatim from the master resume;
3. The finished text is checked again and rejected if it contains any number or proper noun not found in the master resume.

When wording is rewritten toward JD keywords, each rewrite is checked deterministically on its own — no added or altered numbers, no proper nouns outside the master resume, any new keyword must be in your skills list, no significant growth in length — and falls back to the original if it fails. The one-page limit is enforced by rendering, counting PDF pages and trimming in proportion to the overflow, not by asking the model.

### Prompt injection

JDs, emails and company sources are all text anyone can write. The defenses, strongest first:

1. The model that reads raw text is an in-tool call with no tools at all;
2. Dangerous capabilities do not exist in the tool registry;
3. Tool results are marked as untrusted data in the prompt.

Order matters: the first two carry the weight; the prompt-level marking is only a supplement. Planned auto-submission would add a layer of output validation and a destination allow-list — see §3.8 of the design doc.

---

## Cost control

- **Layering**: Sonnet orchestrates; Haiku handles batch JD analysis and email classification.
- **Large text stays out of the agent context**: the agent loop resends the full history every turn, so having the agent read JDs one by one compounds cost.
- **Hard budgets**: `Budget` caps calls and tokens per run; when exceeded, the run stops and reports what it finished.
- **Per-purpose accounting**: every call is written to the `llm_calls` table; `agent spend` breaks cost down by purpose and scheduled task.

The target range is $10–40 per month.

---

## Development

```bash
./.venv/Scripts/python.exe -m pytest -q
```

642 tests, all offline (7 skipped by default, including `--live` smoke tests that hit real ATS APIs). Fixtures are samples captured from the real APIs, which keeps tests deterministic.

Coverage focuses on every edge of the state machine, the append-only triggers on the events table, master-resume id integrity, ATS token extraction, tool boundaries, read-only email, the resume verifier, and every way the shell config could be loosened. A separate set of agent behavior evals (`tests/test_evals.py`) guards what unit tests can't: whether the context blows up, and whether the agent refuses out-of-bounds requests.

Conventions:

- All file I/O goes through `config.read_text` / `config.write_text` to force UTF-8 (the default Windows code page breaks on JD text);
- The `events` table is append-only — database triggers reject UPDATE and DELETE; corrections are made by appending a `status_override` event;
- Status derivation is a pure function that touches neither the database nor the clock, so every state-machine edge can be pinned down in tests.

---

## Project structure

```
config/                 *.example.yaml are committed; same-name *.yaml hold your real content
src/jha/
  cli.py                command-line entry point
  db.py, schema.sql     SQLite database and migrations
  config.py             paths, .env, UTF-8-enforcing file I/O
  profile.py            master resume loading and validation
  sources/              Greenhouse / Lever / Ashby adapters
  filters.py            rule-based screening
  ingest.py             fetch pipeline, delisting detection
  analyze.py            JD analysis (in-tool calls)
  tailor.py             resume selection and rewriting
  render.py             HTML template → PDF, one-page loop
  verify.py             deterministic fabrication checker
  questions.py          three-tier application-question classification
  why.py                per-job "Why this company" drafting
  tracking.py           application tracking, confirmation alerts, export
  status.py             pure event → status derivation
  mail/                 read-only IMAP, prefilter, classification, matching, routing policy
  prep.py               interview prep packs
  notify.py             Discord push
  schedules.py          scheduled-task definitions
  agent/                tools.py (registry = security boundary), loop.py, client.py, persistence.py
  mcp_server.py         shell: per-task scoped MCP server
  openclaw.py           shell: config generation and verification
  tools/resolve_ats.py  ATS type and board-token lookup
tests/                  offline tests and fixtures
data/                   SQLite database, generated resumes (not committed)
```

---

## Roadmap

| Stage | Scope | Status |
|---|---|---|
| Phase 0 | Data model, master resume, validators | Done |
| Phase 1 | Job monitoring | Done |
| Phase 2 | JD analysis and matching | Done |
| Phase 3 | Resume tailoring | Done |
| Phase 4 | Application tracking, application questions | Done |
| Phase 4.5 | Form prefill | Not started; off the critical path |
| Phase 5 | Email processing and status updates | Done |
| Phase 6 | Interview practice | Not implemented |
| Phase 7 | Polish and operations | Ongoing |
| Shell | OpenClaw always-on runtime | Code-complete; awaiting a live trial |
| §3.8 | Closed-loop automation: submission executor, automatic company discovery | Planned |

The full design, decision log and risk register are in [job_hunting_agent_roadmap.md](job_hunting_agent_roadmap.md) (in Chinese).

---

## Disclaimer

A personal project; no open-source license yet.

- Uses only the public, official ATS APIs. No scraping of LinkedIn / Indeed / Glassdoor, and no Easy Apply.
- Applications are submitted by a person. Check the terms of use of any site you target.
- Resumes go out only after human review; legal questions such as work authorization and EEO use only answers you wrote yourself, or are left blank.
