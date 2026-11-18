"""Russian notification templates — no LLM (SPEC §9a.1). Each returns (dedupe key, text,
urgency, buttons). All dynamic values are HTML-escaped."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.core.text import shorten, stable_hash
from agent.notify.events import (
    AssignmentAdded,
    AssignmentRef,
    DueChanged,
    Event,
    FileChanged,
    GradePosted,
    MissingFlagged,
    NewAnnouncement,
    NewComment,
    NewMessage,
)
from agent.notify.outbox import Buttons, Urgency
from agent.telegram.format import fmt_dt, h


@dataclass(frozen=True, slots=True)
class Rendered:
    key: str
    text: str
    urgency: Urgency
    buttons: Buttons | None = None


def deadline_buttons(assignment_id: int) -> Buttons:
    return [
        [
            ("✅ Сделал", f"dl:{assignment_id}:done"),
            ("⏰ Через 2 ч", f"dl:{assignment_id}:snooze"),
            ("🙈 Игнор", f"dl:{assignment_id}:ignore"),
        ]
    ]


def _when(dt: datetime | None, zone: ZoneInfo) -> str:
    return fmt_dt(dt, zone) if dt else "без срока"


def _link(a: AssignmentRef) -> str:
    return f'\n<a href="{h(a.url)}">Открыть в Canvas</a>' if a.url else ""


def _pct(score: float, points: float | None) -> str:
    return f" ({100 * score / points:.0f}%)" if points else ""


def render(event: Event, zone: ZoneInfo, *, announcement_text: str | None = None) -> Rendered:
    match event:
        case AssignmentAdded(a=a):
            text = f"📌 {h(a.course)}: новое задание «{h(a.name)}», срок {_when(a.due_at, zone)}"
            return Rendered(f"new:{a.id}", text, "digest")
        case DueChanged(a=a, old_due=old):
            return Rendered(
                f"due:{a.id}:{a.due_at.isoformat() if a.due_at else 'none'}:{old.isoformat() if old else 'none'}",
                f"📅 {h(a.course)}: срок «{h(a.name)}» изменён: {_when(old, zone)} → "
                f"<b>{_when(a.due_at, zone)}</b>{_link(a)}",
                "normal",
            )
        case GradePosted(
            a=a, score=score, grade=grade, previous_score=prev, stats=stats, comment=comment
        ):
            pts = f"{score:g}/{a.points_possible:g}" if a.points_possible else f"{score:g}"
            lines = [
                f"📊 {h(a.course)}: «{h(a.name)}» — <b>{pts}</b>{_pct(score, a.points_possible)}"
                + (f", {h(grade)}" if grade and grade != f"{score:g}" else "")
                + (f" (было {prev:g})" if prev is not None else "")
            ]
            if stats and {"mean", "min", "max"} <= stats.keys():
                lines.append(
                    f"Группа: среднее {stats['mean']:.1f}, min {stats['min']:g}, max {stats['max']:g}"
                )
            if comment:
                lines.append(f"💬 {h(shorten(comment, 400))}")
            return Rendered(f"grade:{a.id}:{score:g}", "\n".join(lines) + _link(a), "normal")
        case NewComment(a=a, author=author, text=text):
            return Rendered(
                f"comment:{a.id}:{stable_hash(text)}",
                f"💬 {h(a.course)}: комментарий к «{h(a.name)}» от {h(author)}:\n{h(shorten(text, 600))}"
                + _link(a),
                "normal",
            )
        case MissingFlagged(a=a):
            text = (
                f"⚠️ {h(a.course)}: «{h(a.name)}» отмечено как <b>missing</b> "
                f"(срок {_when(a.due_at, zone)}).{_link(a)}"
            )
            return Rendered(f"missing:{a.id}", text, "normal", deadline_buttons(a.id))
        case NewAnnouncement(id=aid, course=course, title=title, text=text, url=url):
            body = announcement_text if announcement_text is not None else shorten(text, 500)
            link = f'\n<a href="{h(url)}">Открыть</a>' if url else ""
            return Rendered(
                f"ann:{aid}", f"📢 <b>{h(course)}</b>: {h(title)}\n{h(body)}{link}", "normal"
            )
        case FileChanged(course=course, name=name, is_new=is_new, stamp=stamp, file_id=file_id):
            verb = "новый файл" if is_new else "обновлён файл"
            return Rendered(
                f"file:{file_id or stable_hash(course + name)}:{'new' if is_new else stamp}",
                f"📄 {h(course)}: {verb} «{h(name)}»",
                "digest",
            )
        case NewMessage(id=cid, subject=subject, preview=preview, participants=people):
            who = f"\n{h(', '.join(p for p in people if p))}" if people else ""
            return Rendered(
                f"msg:{cid}:{stable_hash(preview)}",
                f"✉️ <b>{h(subject)}</b>{who}\n{h(shorten(preview, 300))}",
                "normal",
            )
    raise TypeError(f"Unknown event {type(event).__name__}")
