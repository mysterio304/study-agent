# Study Agent — Architecture & DB Schema (v2, 2026-11-18)

Status: v1 and v2 approved; M1 and M2 implemented.

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
| D8 | **Brain = hand-written Claude tool-use loop** (`AsyncAnthropic`, `tool_choice=auto`), not the SDK's beta tool runner. | We need per-step budget checks, a hard step limit, per-call model routing, prompt-size guards, cost accounting and confirmation gates inside the loop; an explicit loop is also easier to read for a portfolio. |
| D11 | **Haiku by default, Sonnet by rule.** Model choice is a pure function of (command, job type, detected intent, budget state); no LLM call decides routing. Haiku may hand off via an `escalate` tool. | ~4× cheaper per typical day than Sonnet-for-chat (§11.8), and testable. |
| D9 | **Search behind a `Retriever` protocol**; v1 = SQLite FTS5 BM25. | Embeddings can be added later without touching tools. |
| D10 | **`--dry-run`**: Canvas/ElevenLabs replaced by fixture-backed fakes (`fixtures/*.json`), Telegram replaced by a console sink; LLM uses the real API if a key exists, else a scripted stub. Plus a small CLI (`python -m agent.cli plan|sync|need|grades --dry-run`) for demos. | Spec §12; also lets me show you milestone demos before your bot/keys exist. |

## 2. Stack

Python 3.12 · uv (deps + lockfile) · aiogram 3 · APScheduler 3.x · httpx · SQLAlchemy 2 (async) + aiosqlite · Alembic (SQLite batch mode) · pydantic-settings · anthropic · pypdf/pdfplumber, python-pptx, python-docx · ffmpeg · pytest, pytest-asyncio, respx, time-machine · ruff.

LLM: `CHEAP_MODEL=claude-haiku-5-5` by default, `SMART_MODEL=claude-sonnet-5-5` for escalations only; full design in §11.

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

**Replan stability.** A replan is seeded with (a) the current future blocks as an *anchor* — a
small score bonus for keeping the same topic in the same slot/day — and (b) each exam's last
block date, so the keep-warm rule does not fire spuriously. Logged progress and spaced-repetition
state feed the same virtual-progress model the planner uses while allocating, so if the student
follows the plan exactly, the nightly replan changes 0 blocks (tested). Proposal cards state how
many blocks change.

**Non-negotiables as flags.** `eve_review_only`, `no_new_topics_eve`, `protected_sleep` and
`no_blocks_during_exams` default to on, are enforced by the allocator *and* re-checked by the
validators, and can only be switched via `/settings <key> off` (never via chat/tools).

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
| `announcements` | `id` (canvas), `course_id` FK, `title`, `message_html`, `posted_at`, `html_url`, `summary_ru` (set once, only if text > 500 chars) |
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
| `llm_usage` | `id`, `at`, `model`, `purpose` (call type), `route_reason`, `batch` bool, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_5m_tokens`, `cache_write_1h_tokens`, `cost_usd`, `prefix_key` (toolset profile + model, for TTL analysis) |
| `conversation_summary` | `id` = 1 (single row), `text`, `covers_until_message_id`, `updated_at` |
| `quiz_questions` | `id`, `topic_id` FK, `question_json` (stem, options/answer, explanation, citation), `source_chunk_ids`, `origin` (batch/on_demand), `times_used`, `last_used_at`, `retired` bool, `created_at` |
| `llm_batches` | `id`, `anthropic_batch_id`, `purpose`, `status` (submitted/ended/failed/consumed), `request_count`, `submitted_at`, `ended_at` |
| `stt_transcripts` | `audio_sha256` PK, `text`, `model`, `created_at` |
| `chunks` | `id`, `file_id` FK, `course_id` FK, `ordinal`, `locator` ("p. 12" / "slide 7"), `text` |
| `chunks_fts` | FTS5 virtual table (`text`, external content = `chunks`, unicode61 tokenizer), kept in sync by triggers |

Debt and streaks are derived (queries/views), not stored.

## 8. Safety rules → where they are enforced

| Rule | Enforcement |
|---|---|
| Whitelist | aiogram *outer* middleware on every update type; non-owner updates dropped + logged without content. |
| Canvas read-only | `CanvasClient._request()` + httpx request hook; unit test asserts POST/PUT/PATCH/DELETE raise. |
| Plan changes need confirmation | No apply tool exists; `apply_proposal()` only reachable from the callback router; `small_auto` path limited to one missed block. |
| Budgets | Step counter in the loop; `BudgetGuard` reads today's `llm_usage` sum before every API call: ≥ 80 % warn once, ≥ 100 % Haiku-only + templates, ≥ 150 % no LLM until midnight HKT; templated notifications always work. `PromptGuard` enforces per-call-type input budgets and the Haiku 100K ceiling. |
| Non-negotiables | `settings.is_locked`; planner validators run on every candidate plan and reject violations (tests for each). |
| Secrets | `SecretStr` in config, logging redaction filter, `.env` in `.gitignore`, no secrets in fixtures. |

## 9. Testing

Tests first for the pure-logic core: planner (priority, slots, allocation, non-negotiables, feasibility, undo), grade calculator, Canvas client (respx: pagination, 401/403, read-only guard), differ/first-sync seeding, deadline ladder + quiet hours, **router decisions, prompt-size budgets per call type, byte-stability of tools/system prompt, cost accounting**. Target ≥ 80 % coverage on `planner/`, `grades/`, `canvas/`, `notify/`; Telegram handlers get thin smoke tests.

## 10. Deployment

`Dockerfile` (python:3.12-slim + ffmpeg, non-root user) + `docker-compose.yml` (one service, `restart: unless-stopped`, `./data` volume for SQLite + files, `.env` via `env_file`). Until a VPS exists, run locally with `uv run python -m agent` (macOS `caffeinate` to stop sleep). For real 24/7 pings a small VPS is needed (1 vCPU / 1–2 GB RAM is plenty).

## 11. LLM cost design (SPEC §9a)

### 11.1 Where the LLM is (and isn't) used

| No LLM (templates / pure Python) | LLM |
|---|---|
| priorities, plan, replanning, feasibility, grade calc, deadline ladder, diffing; all notifications; `/today` `/plan` `/progress` `/deadlines` `/grades` `/need` `/usage` | free-form chat & voice, plan-change negotiation, plan write-up, check-in parsing, quiz generation/grading, announcement summaries (> 500 chars), topic discovery, diagnostic analysis |

### 11.2 Routing (`brain/router.py`, pure function, unit-tested)

| Trigger | Model | Toolset profile |
|---|---|---|
| free text, default | Haiku | `chat` |
| free text matching plan intent (план, перенес*, сдвин*, забью, отмени, «верни как было», pending proposal open) | Sonnet | `plan` |
| free text matching material intent (объясни, что такое, как работает, почему, a known topic/term) — Haiku first; Sonnet if multi-part or after `escalate` | Haiku → Sonnet | `material` |
| grades/deadlines questions | Haiku | `grades` |
| nightly plan write-up, diagnostic analysis, topic discovery | Sonnet | none (single structured call) |
| check-in parse, summaries, quiz gen/grading | Haiku | none |
| `escalate(reason)` tool call, or Haiku `refusal` | Sonnet, same profile | — |
| budget ≥ 100 % | Haiku only (Sonnet routes downgrade; user told) | — |

### 11.3 Request layout & caching

```
tools (profile, sorted, deterministic JSON)        ┐
system prompt (static, no dates)                   │ byte-stable per (model, profile)
static profile: targets, cutoffs, agreements       ┘ ← explicit cache_control (CACHE_TTL)
rolling summary + last 6–8 messages (plain text)     ← automatic cache_control (moves forward)
newest user turn = dynamic context block + message
```
- Caches are per model and per profile: 2 models × 4 profiles = at most 8 warm prefixes; each prefix is ~3–4K tokens, above the 512-token minimum for both models.
- Agreements change rarely; changing one re-writes that prefix once.
- History holds plain text only (no tool/thinking blocks), so cross-turn history is never "edited" in a way that invalidates cached thinking; within a turn the tool loop is append-only.
- TTL: start on 5m. `/usage cache` replays the logged request gaps per `prefix_key` and prices both TTLs; switch when the data says so (expected: 1h only pays off if ≥ 3 requests share a prefix within an hour with gaps > 5 min).

### 11.4 Per-call-type budgets (asserted in tests)

| Call type | Model | Thinking | `max_tokens` | Input budget (tokens) |
|---|---|---|---|---|
| `chat_turn` (per step) | Haiku | low effort | 1,200 | ≤ 12K (prefix ≤ 4K, context ≤ 1.5K, history ≤ 2.5K, tool results ≤ 1.5K/step) |
| `escalated_turn` (per step) | Sonnet | low effort | 2,500 | ≤ 16K (+ RAG ≤ 2K) |
| `plan_writeup` | Sonnet | low effort | 1,200 | ≤ 8K |
| `checkin_parse` | Haiku | off | 600 | ≤ 3K |
| `history_summary` | Haiku | off | 400 | ≤ 4K |
| `announcement_summary` | Haiku | off | 300 | ≤ 3K (text truncated) |
| `quiz_generate` (Batch) | Haiku | low effort | 1,500 | ≤ 4K |
| `answer_grade` | Haiku | off | 300 | ≤ 2K |
| `diagnostic_analysis` | Sonnet | medium | 2,000 | ≤ 10K |
| `topic_discovery` (Batch) | Sonnet | medium | 3,000 | ≤ 12K |

Token counts are estimated locally with a conservative estimator (UTF-8 bytes ÷ 3, which over-counts Russian text) and cross-checked against `usage.input_tokens` in logs. Over budget → truncate in order: oldest history, RAG chunks, tool-result tails. Any Haiku request estimated ≥ 90K raises before sending (backstop for the 100K price step).

Tool results go through per-tool serialisers that keep only needed fields and cap length (e.g. deadlines: name, course code, due HKT, status — max 15 rows).

### 11.5 Reuse & batching
- `quiz_questions` is a per-topic bank: verification quizzes draw unused/least-used questions first; a nightly job (02:30) submits one Message Batch for tomorrow's planned topics whose bank is short; results are collected by the 1-minute tick. If a block ends before the batch is ready → one on-demand Haiku call.
- Topic discovery and diagnostic-question generation run as Batches during setup.
- STT: transcript cached by audio SHA-256. TTS: only on request / voice mode, gist ≤ 300 chars.

### 11.6 Agent loop
Step limit `MAX_AGENT_STEPS`; independent tool calls run concurrently with `asyncio.gather` and are returned in one message; the loop ends on the first `end_turn`. Every step passes `PromptGuard` and `BudgetGuard` first.

### 11.7 Accounting
Each call writes one `llm_usage` row (cost from a configurable price table, batch at 50 %). `/usage` shows today / 7 days by purpose and model, cache hit rate, and budget state.

### 11.8 Estimated cost per day (Claude API only; ElevenLabs is billed separately)

Assumptions: prefix 3.5K tokens, per-turn tail 2.5K, Haiku chat turn = 2 calls, Sonnet turn = 3 calls with RAG 2K, 7 study blocks/day, prices Haiku $0.10/$0.50, Sonnet $2/$10 per MTok, cache read 0.1×/0.05×, 5-min write 1.25×, Batches 50 %.

| Day | Haiku chat turns | Sonnet turns | Est. cost |
|---|---|---|---|
| light | 10 | 0 | **≈ $0.03** |
| typical | 25 | 4 | **≈ $0.25** |
| heavy | 50 | 10 | **≈ $0.58** |
| one-time setup | topic discovery + diagnostic | | **≈ $0.19** |

Sonnet escalations are ~80 % of a typical day's cost. For comparison, the v1 design (Sonnet for all chat) is ≈ $1.08 on a typical day. Nov 19 → Dec 14 at the typical rate ≈ **$7 total**. Main uncertainty: thinking tokens (billed as output) — if they double output tokens, a typical day is ≈ $0.40. `DAILY_LLM_BUDGET_USD=2.0` therefore acts as a safety net, not a constraint.
