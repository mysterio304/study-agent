# Study Agent

A personal AI agent that lives in Telegram, reads my Canvas LMS (read-only), and **plans and
enforces final-exam preparation**. Telegram is only the interface — the core is a Claude
tool-use agent plus a deterministic planner, with persistent memory and proactive behaviour.

> Status: **Milestone 2 — agent loop + finals planner MVP.** Joint plan for four exams,
> proposals with confirmation + undo, block pings, evening check-in, nightly replanning, and a
> cost-aware Claude agent (Haiku by default, Sonnet by rule, prompt caching, budgets).
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
- **`--dry-run`** — runs on synthetic fixtures without any external API.

## Quick start (local)

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # install deps
uv run pytest                             # run tests
uv run python -m agent.cli map --dry-run  # demo: seeded finals + suggested course mapping
uv run python -m agent.cli plan --dry-run # demo: topics → first joint plan → pings → replan → undo
```

### Run against real services

1. `cp .env.example .env` and fill it in **locally** (never commit or share it):
   - `TELEGRAM_BOT_TOKEN` — create a bot with @BotFather;
   - `TELEGRAM_OWNER_ID` — your numeric id (e.g. from @userinfobot);
   - `CANVAS_TOKEN` — Canvas → Account → Settings → *New Access Token*;
   - `ANTHROPIC_API_KEY`.
2. `uv run python -m agent` — the bot starts long-polling; send `/start`: map exams to courses →
   review discovered topics → rate your level per topic → confirm the first plan.

### Commands

| Command | What it does (LLM?) |
|---|---|
| `/today`, `/plan`, `/progress` | Today / next 3 days / topic levels — templates, no LLM |
| `/usage` | LLM spend today and 7 days, by purpose and model, cache hit rate |
| `/undo` | One-tap proposal to restore the previous plan version |
| `/replan` | Recompute the plan from actual progress (proposal) |
| `/checkin` | Evening check-in now (answer in free text) |
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
  canvas/        read-only client, endpoint layer, fixture fake, sync
  brain/         LLM client (guards, caching, accounting), router, tools, agent loop, batches
  planner/       priority, slots, joint allocator, validators, feasibility, proposals/undo
  prep/          exam seed, course mapping, topic discovery/review, check-in, settings
  notify/        scheduled jobs: tick (pings, reminders), morning, evening, nightly replan
  telegram/      whitelist middleware, /start wizard, commands, chat, cards
  files/ voice/ grades/   (upcoming milestones)
tests/           pytest + respx (HTTP mocked)
fixtures/        synthetic data for --dry-run
```

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest --cov=agent
uv run alembic revision --autogenerate -m "..."   # after changing agent/db/models.py
```
