# Study Agent

A personal AI agent that lives in Telegram, reads my Canvas LMS (read-only), and **plans and
enforces final-exam preparation**. Telegram is only the interface — the core is a Claude
tool-use agent plus a deterministic planner, with persistent memory and proactive behaviour.

> Status: **Milestone 5 — course files & quizzes.** Course files are downloaded, indexed (FTS5)
> and searched with citations; every finished block ends with 2–3 quiz polls from your own
> slides (a block counts fully only if passed); optional diagnostic; timed mock-exam mode.
> Earlier: **Milestone 4 — voice.** Voice notes are transcribed (ElevenLabs Scribe, cached) and
> handled like text; spoken replies on request, in voice mode, or as a morning brief.
> Earlier: **Milestone 3 — notifications.** On top of the finals planner and cost-aware agent
> (M2): Canvas sync with diffing, deadline ladder with buttons, grades with class stats and
> teacher comments, announcements, new files, inbox, quiet-hours outbox, morning digest, weekly
> report, and a grade calculator (`/need`).
> See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and the milestone plan.

## Highlights

- **Read-only by construction** — the Canvas client refuses any non-GET request at two layers
  (method check + httpx request hook) and never sends the token to a foreign host
  (pagination links, file-CDN redirects). Covered by tests.
- **Owner-only bot** — an outer aiogram middleware drops every update not from the owner.
- **UTC everywhere** — a custom SQLAlchemy type rejects naive datetimes; local time (HKT) only
  at the edges.
- **Deterministic planner, LLM for conversation** — priorities, slots, phases and feasibility
  are pure, tested Python; the model explains and proposes, and a plan changes only when you
  press a button. Replans are anchored to the current plan, so following it changes nothing.
- **Token-efficient by design** — rule-based model routing, byte-stable cached prefixes per
  toolset profile, compact context, per-call-type token budgets asserted in tests, Batches for
  offline work, per-call cost accounting with a daily budget (`/usage`).
- **No notification floods** — each Canvas scope's first sync seeds state silently; every
  message has a stable dedupe key; non-urgent messages wait out quiet hours (the 3 h deadline
  step doesn't); low-priority items (new files) go to the morning digest.
- **`--dry-run`** — runs on synthetic fixtures without any external API.

## Quick start (local)

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # install deps
uv run pytest                             # run tests
uv run python -m agent.cli map --dry-run  # demo: seeded finals + suggested course mapping
uv run python -m agent.cli plan --dry-run   # demo: topics → first joint plan → pings → replan → undo
uv run python -m agent.cli notify --dry-run # demo: Canvas changes → grade/announcement/deadline messages
uv run python -m agent.cli study --dry-run  # demo: files → cited search → block quiz → mock exam
```

### Run against real services

1. `cp .env.example .env` and fill it in **locally** (never commit or share it):
   - `TELEGRAM_BOT_TOKEN` — create a bot with @BotFather;
   - `TELEGRAM_OWNER_ID` — your numeric id (e.g. from @userinfobot);
   - `CANVAS_TOKEN` — Canvas → Account → Settings → *New Access Token*;
   - `ANTHROPIC_API_KEY`.
2. `uv run python -m agent.preflight` — one read-only call per service to verify credentials.
3. `uv run python -m agent` — the bot starts long-polling; send `/start`: map exams to courses →
   review discovered topics → rate your level per topic → confirm the first plan.

### Commands

| Command | What it does (LLM?) |
|---|---|
| `/today`, `/plan`, `/progress` | Today / next 3 days / topic levels — templates, no LLM |
| `/deadlines`, `/grades` | Upcoming Canvas deadlines; course scores and recent grades — templates |
| `/need <course> [grade]` | Final-exam score needed per letter (two scenarios for ungraded work) |
| `/usage` | LLM spend today and 7 days, by purpose and model, cache hit rate |
| `/undo` | One-tap proposal to restore the previous plan version |
| `/replan` | Recompute the plan from actual progress (proposal) |
| `/checkin` | Evening check-in now (answer in free text) |
| `/voice on\|off`, `/voice brief on\|off` | Voice mode (voice in → voice out); morning voice brief |
| `/mock` or «начинаю past paper» | Mock exam with a timer for the real exam length → score → weak topics |
| `/diagnostic <course>` | 2 quiz questions per topic → proposed level changes (button to apply) |
| PDF / PPTX / DOCX file | Indexed as a past paper or course material for that course |
| voice note | Transcribed (shown briefly), then handled like text; «ответь голосом» → spoken gist |
| `/settings [key value]` | Show settings; change one explicitly (the only way to change 🔒 rules) |
| free text | The agent: Haiku by default, Sonnet for plan negotiation / complex material |

`uv run python -m agent --dry-run` uses fixture Canvas data and a separate `data/dry_run.db`
(with Telegram if a token is configured, otherwise it prints a console demo).

## Deploy (24/7)

On a small Ubuntu VPS with Docker:

```bash
git clone <repo> study-agent && cd study-agent
cp .env.example .env && nano .env      # fill in secrets on the server
docker compose up -d --build
docker compose logs -f
```

The SQLite DB and downloaded files live in `./data` (a volume), so rebuilds keep state.

## Project layout

```
agent/
  config.py      settings from .env (secrets as SecretStr)
  app.py         composition root (DB, Canvas source, clock)
  main.py        entrypoint: scheduler + Telegram bot
  cli.py         demo/ops commands
  core/          clock, time helpers, secret-redacting logging
  db/            models, UTC type, Alembic migrations
  canvas/        read-only client, endpoints, normalizer, differ, sync runner, fixture fake
  brain/         LLM client (guards, caching, accounting), router, tools, agent loop, batches
  planner/       priority, slots, joint allocator, validators, feasibility, proposals/undo
  prep/          exam seed, course mapping, topic discovery/review, check-in, settings
  notify/        events, templates, quiet-hours outbox, deadline ladder, jobs, weekly report
  grades/        grade calculator (weighted / points-based) and grade views
  telegram/      whitelist middleware, /start wizard, commands, chat, cards
  voice/         ElevenLabs STT/TTS client, OGG/Opus helpers (ffmpeg only if needed), voice service
  files/         extraction (PDF/PPTX/DOCX), chunking with locators, FTS5 search, indexer
tests/           pytest + respx (HTTP mocked)
fixtures/        synthetic data for --dry-run
```

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest --cov=agent
uv run alembic revision --autogenerate -m "..."   # after changing agent/db/models.py
```
