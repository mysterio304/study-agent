# Personal Study Agent — Build Spec for Claude Code

## 0. Context

I'm Dauren, Year 3 CS student at City University of Hong Kong (CityUHK). I want a **personal AI agent** that lives in Telegram, reads my Canvas LMS, and **plans and enforces my final exam preparation**. Telegram is only the interface — the core is an LLM agent with tools, memory, and proactive behaviour.

- Agent language with me: **Russian** (course material and terms stay in English: TCP, Dijkstra, Big-O, etc.).
- Timezone: **Asia/Hong_Kong (UTC+8)** everywhere. Store UTC, display HKT.
- Single user. Today is 2026-11-18; the first exam is on 2026-12-08, so **the finals planner is the most time-critical feature**.

### My finals (seed these into the DB)

| Course | Date | Time (HKT) | Final weight |
|---|---|---|---|
| Computer Networks | 2026-12-08 | 09:30–11:30 | 70% |
| Design and Analysis of Algorithms | 2026-12-09 | 18:30–20:30 | unknown → find in syllabus or ask |
| Algebra | 2026-12-12 | 18:30–20:30 | unknown → find in syllabus or ask |
| Computer Vision for Interactivity (CS4187) | 2026-12-14 | 14:00–16:00 | unknown → find in syllabus or ask |

Note: Networks and Algorithms are back-to-back (~31 h apart). Both must be essentially ready by Dec 8.

---

## 1. Stack & project layout

- Python 3.12, **aiogram 3**, **APScheduler** (AsyncIOScheduler), **httpx** (async), **SQLite** (SQLAlchemy 2 async or aiosqlite — your choice, keep it simple), **anthropic** SDK (tool use), ffmpeg.
- Text extraction: pypdf / pdfplumber, python-pptx, python-docx.
- Search over course files: **SQLite FTS5 (BM25)** first; leave an interface so embeddings can be added later.
- Tests: pytest + respx (mock HTTP).
- Deploy: Docker Compose (or systemd) on a small Ubuntu VPS, 24/7.

Suggested layout (adjust if you have a better one, but explain why):

```
agent/
  main.py            # startup, scheduler, dispatcher
  config.py          # pydantic-settings from .env
  db/                # models, migrations (simple), repositories
  canvas/            # read-only client, sync jobs, diffing
  telegram/          # handlers, keyboards, whitelist middleware
  brain/             # agent loop, tools, system prompt, memory
  planner/           # priority, phases, scheduling, replanning, plan versions
  prep/              # exams, topics, levels, sessions, diagnostics, quizzes
  voice/             # ElevenLabs STT + TTS, ffmpeg conversion
  files/             # download, text extraction, FTS index
  notify/            # reminders, digests, quiet-hours outbox
tests/
.env.example
docker-compose.yml
README.md
```

## 2. Configuration (.env)

Create `.env.example` with placeholders only. **Never ask me to paste secrets into chat. Never log secrets. Never commit `.env`.**

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_ID=
CANVAS_BASE_URL=            # e.g. https://<cityu canvas domain>
CANVAS_TOKEN=
ELEVENLABS_API_KEY=
ELEVENLABS_STT_MODEL=scribe_v2
ELEVENLABS_TTS_MODEL=       # current multilingual model, check ElevenLabs docs
ELEVENLABS_VOICE_ID=
ANTHROPIC_API_KEY=
AGENT_MODEL=claude-sonnet-5-5
CHEAP_MODEL=claude-haiku-5-5
TZ=Asia/Hong_Kong
DAILY_STUDY_CAP_HOURS=6.5   # rises to 8.5 in the final week before Dec 8 (configurable)
QUIET_HOURS=00:30-08:00     # placeholder, I'll confirm
MORNING_DIGEST_TIME=08:30
EVENING_CHECKIN_TIME=21:30
AUTONOMY_MODE=small_auto    # small_auto | confirm_all
MAX_AGENT_STEPS=8
DAILY_LLM_BUDGET_USD=2.0
```

## 3. Hard safety rules (enforce in code, not only in the prompt)

1. **Whitelist**: middleware drops every update not from `TELEGRAM_OWNER_ID`.
2. **Canvas is read-only**: the Canvas client must refuse any method other than `GET` (raise an exception). File downloads are GET and allowed. No submissions, posts, discussion replies, profile changes — ever.
3. **Plan changes require confirmation**: the agent can *propose* a change (with a diff and options A/B), but applies it only after I press a button. Exception: if `AUTONOMY_MODE=small_auto`, it may move a single missed block to the next free slot and must notify me.
4. **Budgets**: max `MAX_AGENT_STEPS` tool calls per user turn; track daily LLM spend; when the daily budget is hit, degrade gracefully (templated notifications still work, agent says it's over budget).
5. **Non-negotiables in the planner** (can only be changed via an explicit settings command, never through casual chat): review session the day before each exam; no new topics the day before an exam; protected sleep before the 09:30 exam on Dec 8; no study blocks during exams.

## 4. Canvas integration (read-only)

- Pagination via the `Link` header. Handle 401/403 gracefully (some tabs are hidden for students).
- Endpoints to use (verify against Canvas API docs):
  - `GET /api/v1/courses?enrollment_state=active&include[]=total_scores`
  - `GET /api/v1/courses/:id/assignments?include[]=submission&include[]=score_statistics`
  - `GET /api/v1/courses/:id/assignment_groups?include[]=assignments` (weights for grade calculator)
  - `GET /api/v1/users/self/missing_submissions`
  - `GET /api/v1/announcements?context_codes[]=course_:id`
  - `GET /api/v1/courses/:id/files` **and fallback** `GET /api/v1/courses/:id/modules?include[]=items` (when the Files tab is hidden, files are often still reachable via module items)
  - `GET /api/v1/conversations` (inbox)
  - Syllabus: `GET /api/v1/courses/:id?include[]=syllabus_body` (to find final exam weights)
- **Polling** every 10–15 min (Canvas has no webhooks for students). Store snapshots; diff old vs new.
- **First sync seeds state silently** — no notification flood.
- Map Canvas courses to my 4 exams (fuzzy match by name, then ask me to confirm).

## 5. Notifications

- **Deadline ladder**: 7d, 3d, 24h, 3h before due, only if not submitted. Inline buttons: «Сделал» / «Напомни через 2 ч» / «Игнор».
- **Missing submissions** alert.
- **New grades**: score, grade, teacher comment, and class mean/min/max if `score_statistics` is available.
- **Announcements**: short Russian summary + link.
- **New files** in a course; **inbox messages**; **submission comments**.
- **Quiet hours**: non-urgent messages go to an outbox and are flushed after quiet hours. The 3h deadline reminder is urgent and bypasses quiet hours.

## 6. Finals prep — the core feature

### Data
- `exams` (course, datetime, duration, final weight, target grade, current course score)
- `topics` (exam, name, source refs to files/modules, est. effort hours, exam importance, level 0–3, last_studied_at, next_review_at)
  - Levels: 0 = untouched, 1 = read, 2 = can solve problems, 3 = can explain to someone else.
- `study_sessions` (topic, start, end, minutes, kind: learn/practice/review/mock, verified bool, quiz score)
- `plan_versions` (full plan snapshot + reason + who proposed + applied_at) → supports undo.
- `blocks` (date, start, end, topic, kind, status: planned/started/done/skipped/moved)
- `agreements` (free-text rules we agreed on, e.g. "don't touch Network Layer until Nov 25")

### Topic discovery
Build an initial topic list per course from Canvas modules, lecture file names, and syllabus. Show it to me for edits before using it. Allow adding/removing/merging topics via chat.

### Diagnostic
On setup, run a short diagnostic per course (2–3 questions per topic, generated from the course files) to set initial levels. I can skip and self-rate instead.

### Priority (per topic, recomputed nightly)
Combine (document the formula and make weights configurable):
- **exam stakes** — from the grade calculator: how much the final can move my letter grade vs my target;
- **gap** — target level minus current level;
- **topic importance** — frequency in past papers / lecture coverage;
- **urgency** — days left until that exam;
- **forgetting** — time since last study (spaced repetition: review at +1, +3, +7 days).

### Phases per exam
1. **Learn** — close gaps on weak topics.
2. **Practice** — problems and past papers.
3. **Final week** — timed mock exams + review. Day before exam = review only.

Plan all four exams **jointly**, not one by one. Expected shape (planner decides details):
- Nov 19 – ~Dec 1: learning across all four, Networks + Algorithms prioritized, Algebra/CV kept warm.
- Dec 2–7: practice + mocks for Networks & Algorithms; Algebra/CV in maintenance.
- Dec 8: exam in the morning, then light Algorithms review + rest.
- Dec 9: Algorithms final review, exam in the evening.
- Dec 10–12: Algebra (exam evening of Dec 12).
- Dec 13–14: CV (exam afternoon of Dec 14).

### Scheduling
- Fill each day up to `DAILY_STUDY_CAP_HOURS` with ~50-min blocks + breaks; one lighter day per week; respect quiet hours and exam times.
- **Nightly replanning** from actual progress. Missed work becomes visible "debt" and is redistributed.
- If the plan becomes infeasible, don't silently overload me — say so and propose what to drop with minimal expected loss of marks.

### Enforcement
- **Morning**: today's plan as blocks with times.
- **Block start ping** with «Начал» / «Перенести». No answer → re-ping after 15 min, escalate gently.
- **Block end**: 2–3 verification questions on that topic (from course files). A block counts only if passed; results update the topic level.
- **Evening check-in** is mandatory (text or voice): "что сделал сегодня?" → parsed into sessions. If skipped, next morning starts with it.
- Streaks, weekly report (optionally forwarded to an accountability contact — off by default).
- **Mock exam mode**: "начинаю past paper" → timer for the real exam duration → ask score + which topics I lost marks on.

### Grade calculator
Using assignment group weights and graded/ungraded work, compute the score needed on the final for each target grade. Handle unweighted (points-based) courses. Say clearly that it's an estimate (drop rules, curves, etc.).

## 7. Course files & RAG

- Download course files (PDF, PPTX, DOCX), extract text, chunk with source refs (file, page/slide), index in FTS5.
- Agent tool `search_course_files(course, query)` returns chunks with citations.
- Used for: topic discovery, diagnostics, verification quizzes, answering my questions about material.
- Quizzes can use Telegram native quiz polls where it fits.
- Detect new/updated files on each sync and re-index.

## 8. Voice

- **Input**: Telegram voice (OGG/Opus) → ElevenLabs Speech-to-Text (`ELEVENLABS_STT_MODEL`). Convert with ffmpeg only if needed. Pass the transcript to the agent as a normal message. Show me the transcript briefly.
- **Output (TTS)**: only when I ask ("ответь голосом"), when voice mode is on (voice in → voice out), or for the optional morning voice brief.
  - Rule: **voice = short gist, text = details** (schedules/lists always also sent as text).
  - Send via `sendVoice` as OGG/Opus (convert with ffmpeg if the TTS format differs).
  - Keep voice replies short (TTS is billed per character).
- Check current ElevenLabs API docs for model IDs and output formats; keep them configurable.

## 9. The agent (brain)

- Claude tool-use loop with a step limit. Tools (at least):
  - Canvas: `get_deadlines`, `get_grades`, `get_course_scores`, `get_announcements`, `list_course_files`
  - Files: `search_course_files`
  - Prep: `get_progress`, `log_study_session`, `set_topic_level`, `get_plan(date_range)`, `propose_plan_change`, `explain_priority(topic|day)`
  - Grades: `calc_needed_final(course, target)`
  - Reminders: `add_personal_reminder(text, at)`, `list_reminders`
  - Quiz: `start_quiz(topic, n)`
  - Memory: `save_agreement`, `list_agreements`
  - Voice: `reply_with_voice(text)`
- `propose_plan_change` returns a proposal with options and a diff; applying happens only via button callback (`apply_plan_change(proposal_id, option)`), never by the LLM directly. Provide undo ("верни как было").
- **Persona/behaviour** (system prompt):
  - Speaks Russian, concise, direct; course terms in English.
  - Honest, not sycophantic: if I ask for something risky ("забью на сети до 3 декабря"), it states the consequences clearly; the final decision is mine.
  - Can explain *why* the plan is what it is.
  - Proactive but not spammy.
- Context per turn: today's date/time HKT, upcoming exams + days left, today's plan, debt, recent sessions, relevant agreements, last N messages. Keep it compact.
- Use `CHEAP_MODEL` for summaries and quiz generation, `AGENT_MODEL` for the conversation loop.

## 10. Commands (besides free-form chat/voice)

`/start` (setup wizard: course mapping, topic review, diagnostic, settings), `/today`, `/plan`, `/progress`, `/deadlines`, `/grades`, `/need <course> <grade>`, `/voice on|off`, `/settings`, `/undo`.

## 11. Milestones (commit after each, show me a short demo/test output)

Order is driven by the Dec 8 deadline:

1. **Skeleton**: config, DB, whitelist, read-only Canvas client with tests, course mapping, deploy setup (Docker) so it can run 24/7 early.
2. **Agent loop + finals planner MVP**: exams seeded, topic discovery + my review, self-rating (diagnostic can come later), priority + daily plan, morning plan, block pings, evening check-in, nightly replanning, plan proposals with confirmation + undo. **Target: usable ASAP.**
3. **Notifications**: deadlines ladder with buttons, grades, announcements, files, inbox, quiet-hours outbox, digests.
4. **Voice**: STT input, TTS output modes.
5. **Files/RAG**: download, extraction, FTS search, verification quizzes, diagnostic, mock exam mode.
6. **Hardening**: budgets, error handling/retries/backoff for Canvas & APIs, logging without secrets, README with setup steps.

## 12. How I want you to work

- Start by reading this spec, then **ask me any clarifying questions in one batch** and propose the architecture + DB schema before writing code.
- Verify external API details (Canvas, ElevenLabs, Telegram Bot API, Anthropic) against current docs instead of guessing.
- Write tests for the planner, grade calculator, Canvas client (mocked), and diffing logic.
- Provide a `--dry-run` mode that uses fixture data instead of real APIs.
- Keep the code clean and documented — this is also a portfolio project for internship applications.

## 13. Open decisions (ask me if still unset)

- Daily study cap (default 6–7 h now, 8–9 h in the final week).
- `AUTONOMY_MODE`: small auto-moves allowed or confirm everything.
- Quiet hours.
- Target grades per course.
- Accountability contact for weekly reports (optional).
