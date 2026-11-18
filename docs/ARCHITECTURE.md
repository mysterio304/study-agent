# Study Agent — Architecture & DB Schema (proposal v1, 2026-11-18)

Status: **awaiting approval**. Nothing below is implemented yet.

## 1. Guiding decisions

| # | Decision | Why |
|---|---|---|
| D1 | **One asyncio process**: aiogram 3 long-polling + APScheduler `AsyncIOScheduler` + httpx, all on one event loop. | Single user, no public HTTPS needed (works on a Mac and on a VPS unchanged). |
| D2 | **The planner is deterministic Python, not the LLM.** The LLM explains, discusses and *proposes*; the planner computes priorities and blocks. | Testable, reproducible, cheap, and the hard rules (non-negotiables, caps) are enforced by code, not by prompt. |
| D3 | **DB is the source of truth for everything time-based.** The scheduler runs a few fixed cron jobs plus a **1-minute tick** that reads due items (block pings, reminders, deadline-ladder steps, outbox) from the DB. All sends are idempotent via `dedupe_key`. | No lost jobs after restarts/crashes; no duplicate pings. |
| D4 | **Times stored as UTC** (custom `UTCDateTime` SQLAlchemy type that rejects naive datetimes); converted to HKT only at the edges (prompt context, Telegram text). A `Clock` object is injected everywhere `now()` is needed. | Spec rule + makes the planner/ladder tests deterministic. |
| D5 | **Canvas read-only is enforced at transport level**: the client's single `_request()` raises `ReadOnlyViolation` for any method ≠ GET, and an httpx event hook re-checks every outgoing request (including redirects). | Rule 3.2 — impossible to bypass from higher layers. httpx also drops `Authorization` on cross-origin redirects (Canvas file downloads redirect to a file host), so the token never leaks. |
| D6 | **Plan changes go through `proposals`**; only the Telegram callback handler can call `apply_proposal()`. The LLM has `propose_*` tools but no apply tool. | Rule 3.3 enforced structurally. |
| D7 | **Runtime settings live in the DB** (`settings` table) layered over `.env` defaults. Cap/window/etc. can be changed by discussing with the agent → it creates a *settings proposal* → you tap ✅. Non-negotiables are `locked` and only change via `/settings`. | Your answers 1–2 + rule 3.5. |
| D8 | **Brain = hand-written Claude tool-use loop** (`AsyncAnthropic`, `tool_choice=auto`), not the SDK's beta tool runner. | We need per-step budget checks, a hard step limit, cost accounting and confirmation gates inside the loop; an explicit loop is also easier to read for a portfolio. |
| D9 | **Search behind a `Retriever` protocol**; v1 = SQLite FTS5 BM25. | Embeddings can be added later without touching tools. |
| D10 | **`--dry-run`**: Canvas/ElevenLabs replaced by fixture-backed fakes (`fixtures/*.json`), Telegram replaced by a console sink; LLM uses the real API if a key exists, else a scripted stub. Plus a small CLI (`python -m agent.cli plan|sync|need|grades --dry-run`) for demos. | Spec §12; also lets me show you milestone demos before your bot/keys exist. |

## 2. Stack

Python 3.12 · uv (deps + lockfile) · aiogram 3 · APScheduler 3.x · httpx · SQLAlchemy 2 (async) + aiosqlite · Alembic (SQLite batch mode) · pydantic-settings · anthropic · pypdf/pdfplumber, python-pptx, python-docx · ffmpeg · pytest, pytest-asyncio, respx, time-machine · ruff.

LLM: `AGENT_MODEL=claude-sonnet-5-5` (conversation loop), `CHEAP_MODEL=claude-haiku-5-5` (summaries, quiz generation, check-in parsing). Prompt caching on the frozen system prompt + tool list; the volatile per-turn context goes after the cache breakpoint. Server-side refusal fallback enabled. Cost is computed from `usage` × a configurable price table → `llm_usage`.

## 3. Layout

Kept the spec's layout, with three additions (marked ＋):

```
agent/
  main.py            # wiring: DI container, scheduler jobs, dispatcher, --dry-run
  config.py          # pydantic-settings (.env) — secrets as SecretStr
  cli.py           ＋ # demo/ops CLI: plan, sync, need, grades (works with --dry-run)
  core/            ＋ # clock, tz helpers, logging with secret-redaction filter, errors
  db/                # engine/session, models, repositories, alembic env
  canvas/            # read-only client, pagination, sync jobs, normaliser, differ
  telegram/          # routers, keyboards, CallbackData, whitelist middleware, sender
  brain/             # loop, tool registry + schemas, system prompt, context builder, budget
  planner/           # priority, phases, slot builder, allocator, feasibility, versions/undo
  prep/              # exams, topics, sessions, check-in parsing, quizzes, mock mode
  grades/          ＋ # grade calculator (weighted + points-based), letter cutoffs
  voice/             # ElevenLabs STT/TTS, ffmpeg conversion
  files/             # download, extraction, chunking, FTS5 Retriever
  notify/            # deadline ladder, digests, quiet-hours outbox, renderers (RU)
fixtures/            # dry-run Canvas/ElevenLabs fixtures
tests/               # mirrors agent/
docs/ARCHITECTURE.md
.env.example  Dockerfile  docker-compose.yml  README.md
```

`grades/` is split out of `prep/` because it is pure logic used by the planner, a command and a tool — it has no prep state.

## 4. Runtime flows

**Scheduler jobs** (all HKT):
- `canvas_sync` every 12 min → normalise → diff vs DB → events → `notify` (first sync per scope seeds silently).
- `tick` every 1 min → block-start pings/re-pings, block-end verification, personal reminders, deadline ladder steps, outbox flush (respects quiet hours).
- `morning_digest` 08:30, `evening_checkin` 21:30, `nightly_replan` 03:30, `weekly_report` Sun 20:00.

**Agent turn**: message (text or STT transcript) → whitelist middleware → budget check → context builder (date/time HKT, exams + days left, today's blocks, debt, last sessions, active agreements, last N messages) → loop (≤ `MAX_AGENT_STEPS` tool calls) → reply (text, plus TTS gist if voice mode).

**Plan change**: `propose_plan_change` → planner builds candidate plans → `proposals` row with options A/B and a human diff → inline buttons → callback → `apply_proposal()` → new `plan_versions` row. `/undo` / «верни как было» = new proposal that restores the previous version (also one tap).

**Nightly replan** (03:30): recompute levels/debt → build next version → *proposal* shown at the top of the morning digest with one ✅ button. Exception (`small_auto`): moving a single missed block to the next free slot is applied immediately with a notification.

## 5. Planner

### Priority (per topic, recomputed nightly; weights in `settings`)

```
priority = w_s·stakes + w_g·gap + w_i·importance + w_u·urgency + w_f·forgetting      (each term ∈ [0,1])

stakes      = final_weight × clamp(needed_final% for target / 100, 0.3, 1.0)   # unknown weight → 0.5
gap         = (target_level − level) / 3                                     # target_level default 2, 3 for importance=3
importance  = importance / 3                                                 # 1..3: past-paper frequency / lecture coverage
urgency     = exp(−days_until_exam / 7)
forgetting  = 0 if not due, else min(1, 0.5 + overdue_days / 3)             # reviews at +1, +3, +7, +14 days

defaults: w_s .25, w_g .30, w_i .15, w_u .20, w_f .10
```
`explain_priority(topic|day)` returns each term so the agent can say *why*.

### Phases (per exam, from days left)
`learn` (> 7 days) → `practice` (2–7 days: problems, past papers, mocks) → `review` (day before: review/mock only, no level-0 topics).

### Daily scheduling
1. **Slots**: study window 09:00–23:00 minus busy slots (lectures until Nov 21), minus exams ± buffer (1 h before, 30 min after), minus protected sleep (Dec 7: last block ends 21:00); cut into 50 + 10 min blocks with a 30 min break after every 3; capacity = cap hours (Sunday ×0.5; cap rises to 8.5 h on Dec 1–7, 7.5 h on Dec 10–13).
2. **Exam shares**: proportional to the sum of each exam's top topic priorities, with a maintenance floor (every not-yet-taken exam gets ≥ 1 block per 2 days) and a focus override (day before exam E → only E).
3. **Fill**: greedy by priority, re-scoring after each pick (virtual gap shrinks), ≤ 2 blocks per topic per day, block `kind` from phase + level + review due.
4. **Feasibility**: cumulative required hours (per exam, in exam order) vs cumulative capacity. If short → don't overload; build a *drop proposal* ranking topics by expected marks lost per hour saved.
5. **Debt**: skipped blocks of the current version that are not yet re-placed; shown in morning/evening messages and redistributed by the next replan.

Plan horizon: today → Dec 14, regenerated each night; only today + tomorrow are shown as "firm".

## 6. Grade calculator

Weighted groups (`apply_assignment_group_weights`) or points-based. Final exam = its own group if Canvas has one, otherwise the exam's `final_weight` from the syllabus or from you. Ungraded non-final work is shown both pessimistically (0) and at your current average. Output: final % needed for each letter in your cutoff table (A+ ≥ 90, A ≥ 85, A− ≥ 80, then B+ 75, B 70, B− 65, C+ 60, C 55, C− 50, D 45). Always labelled as an estimate (drop rules, curving, rounding).

## 7. DB schema (SQLite)

All `*_at` columns are UTC. `id` is an integer PK unless noted; Canvas entities use the Canvas id as PK.

### Config & time
| table | columns |
|---|---|
| `settings` | `key` PK, `value_json`, `is_locked` bool, `updated_at`, `updated_by` (env/user/proposal) |
| `busy_slots` | `id`, `weekday` (0–6, null if dated), `date` (nullable), `start_local`, `end_local`, `label`, `valid_from`, `valid_until` |

### Canvas mirror
| table | columns |
|---|---|
| `courses` | `id` (canvas), `name`, `course_code`, `current_score`, `current_grade`, `apply_group_weights` bool, `syllabus_html`, `content_hash`, `synced_at` |
| `assignment_groups` | `id` (canvas), `course_id` FK, `name`, `group_weight`, `rules_json`, `content_hash` |
| `assignments` | `id` (canvas), `course_id` FK, `group_id` FK, `name`, `due_at`, `points_possible`, `html_url`, `submission_state`, `submitted_at`, `score`, `grade`, `grader_comment_count`, `is_missing`, `stats_json` (mean/min/max), `user_state` (null/done/ignored), `content_hash`, `synced_at` |
| `announcements` | `id` (canvas), `course_id` FK, `title`, `message_html`, `posted_at`, `html_url`, `summary_ru` |
| `conversations` | `id` (canvas), `subject`, `last_message`, `last_message_at`, `workflow_state`, `content_hash` |
| `course_files` | `id`, `canvas_file_id` (nullable, unique), `course_id` FK, `display_name`, `content_type`, `size`, `canvas_updated_at`, `source` (files_api/module_item/telegram_upload), `kind` (lecture/tutorial/past_paper/other), `local_path`, `sha256`, `index_state` (pending/indexed/failed/unsupported), `indexed_at` |
| `sync_state` | `scope` PK (e.g. `assignments:123`), `initialized` bool, `last_success_at`, `last_error`, `consecutive_failures` |

### Finals prep
| table | columns |
|---|---|
| `exams` | `id`, `course_id` FK nullable, `mapping_confirmed` bool, `title`, `starts_at`, `duration_min`, `final_weight` nullable, `weight_source` (syllabus/user/unknown), `target_grade` (default A−), `location` |
| `topics` | `id`, `exam_id` FK, `name`, `description`, `source_refs_json` (file ids + pages/slides, module ids), `effort_hours`, `importance` 1–3, `level` 0–3, `target_level`, `status` (draft/active/dropped/merged), `merged_into_id`, `last_studied_at`, `next_review_at`, `review_step` |
| `blocks` | `id`, `plan_version_id` FK, `starts_at`, `ends_at`, `exam_id` FK, `topic_id` FK, `kind` (learn/practice/review/mock), `status` (planned/started/done/skipped/moved), `moved_to_block_id`, `ping_count`, `last_ping_at`, `verification_quiz_id` FK |
| `plan_versions` | `id`, `created_at`, `parent_id` FK, `reason`, `proposed_by` (planner/agent/user/auto), `applied_at`, `snapshot_json` (future blocks at apply time) |
| `proposals` | `id`, `created_at`, `kind` (plan/settings/topics/drop), `summary_ru`, `options_json` (per option: label, diff, payload), `status` (pending/applied/rejected/expired/superseded), `chosen_option`, `expires_at`, `tg_message_id` |
| `study_sessions` | `id`, `topic_id` FK nullable, `block_id` FK nullable, `started_at`, `ended_at`, `minutes`, `kind`, `verified` bool, `quiz_score`, `source` (block/checkin/manual/mock), `note` |
| `checkins` | `date_local` PK, `status` (pending/done/skipped), `raw_text`, `parsed_json`, `completed_at` |
| `quizzes` | `id`, `topic_id` FK, `block_id` FK nullable, `purpose` (verification/diagnostic/adhoc), `questions_json` (with citations), `answers_json`, `score`, `passed`, `created_at`, `finished_at` |
| `mock_exams` | `id`, `exam_id` FK, `file_id` FK nullable, `started_at`, `ends_at`, `finished_at`, `score_pct`, `lost_topics_json` |
| `agreements` | `id`, `text`, `exam_id` FK nullable, `active` bool, `valid_until` nullable, `created_at` |

Plan versioning: applying a version replaces only **future `planned`** blocks; past/started/done blocks are history and never rewritten. `snapshot_json` makes undo exact.

### Notifications & reminders
| table | columns |
|---|---|
| `outbox` | `id`, `dedupe_key` UNIQUE (e.g. `deadline:123:24h`), `urgency` (urgent/normal/digest), `text`, `buttons_json`, `as_voice` bool, `not_before`, `status` (pending/sent/failed/cancelled), `attempts`, `sent_at`, `tg_message_id`, `created_at` |
| `reminders` | `id`, `text`, `remind_at`, `status` (pending/sent/done/cancelled), `snoozed_from_id`, `source` (user/deadline_snooze) |

### Brain & files
| table | columns |
|---|---|
| `messages` | `id`, `role`, `content_json`, `via` (text/voice/system), `created_at` |
| `llm_usage` | `id`, `at`, `model`, `purpose`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd` |
| `chunks` | `id`, `file_id` FK, `course_id` FK, `ordinal`, `locator` ("p. 12" / "slide 7"), `text` |
| `chunks_fts` | FTS5 virtual table (`text`, external content = `chunks`, unicode61 tokenizer), kept in sync by triggers |

Debt and streaks are derived (queries/views), not stored.

## 8. Safety rules → where they are enforced

| Rule | Enforcement |
|---|---|
| Whitelist | aiogram *outer* middleware on every update type; non-owner updates dropped + logged without content. |
| Canvas read-only | `CanvasClient._request()` + httpx request hook; unit test asserts POST/PUT/PATCH/DELETE raise. |
| Plan changes need confirmation | No apply tool exists; `apply_proposal()` only reachable from the callback router; `small_auto` path limited to one missed block. |
| Budgets | Step counter in the loop; `BudgetGuard` checks today's `llm_usage` sum before every API call; over budget → templated reply, templated notifications keep working. |
| Non-negotiables | `settings.is_locked`; planner validators run on every candidate plan and reject violations (tests for each). |
| Secrets | `SecretStr` in config, logging redaction filter, `.env` in `.gitignore`, no secrets in fixtures. |

## 9. Testing

Tests first for the pure-logic core: planner (priority, slots, allocation, non-negotiables, feasibility, undo), grade calculator, Canvas client (respx: pagination, 401/403, read-only guard), differ/first-sync seeding, deadline ladder + quiet hours. Target ≥ 80 % coverage on `planner/`, `grades/`, `canvas/`, `notify/`; Telegram handlers get thin smoke tests.

## 10. Deployment

`Dockerfile` (python:3.12-slim + ffmpeg, non-root user) + `docker-compose.yml` (one service, `restart: unless-stopped`, `./data` volume for SQLite + files, `.env` via `env_file`). Until a VPS exists, run locally with `uv run python -m agent` (macOS `caffeinate` to stop sleep). For real 24/7 pings a small VPS is needed (1 vCPU / 1–2 GB RAM is plenty).
