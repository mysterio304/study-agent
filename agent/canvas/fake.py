"""Fixture-backed `CanvasSource` for --dry-run and tests. Reads fixtures/canvas/*.json."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from agent.canvas.api import CanvasCourse

DEFAULT_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "canvas"


class FakeCanvasSource:
    def __init__(self, root: Path = DEFAULT_FIXTURES) -> None:
        self._root = root

    def _load(self, name: str, default: Any = None) -> Any:
        path = self._root / f"{name}.json"
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    async def list_courses(self) -> list[CanvasCourse]:
        return [CanvasCourse.from_api(c) for c in self._load("courses", [])]

    async def get_course(self, course_id: int, *, with_syllabus: bool = True) -> CanvasCourse:
        for raw in self._load("courses", []):
            if raw["id"] == course_id:
                course = CanvasCourse.from_api(raw)
                return course if with_syllabus else replace(course, syllabus_html=None)
        raise KeyError(course_id)

    async def list_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return self._load(f"assignments_{course_id}", [])

    async def list_assignment_groups(self, course_id: int) -> list[dict[str, Any]]:
        return self._load(f"assignment_groups_{course_id}", [])

    async def list_missing_submissions(self) -> list[dict[str, Any]]:
        return self._load("missing_submissions", [])

    async def list_announcements(self, course_ids: list[int], since: date) -> list[dict[str, Any]]:
        wanted = {f"course_{cid}" for cid in course_ids}
        return [a for a in self._load("announcements", []) if a.get("context_code") in wanted]

    async def list_files(self, course_id: int) -> list[dict[str, Any]] | None:
        return self._load(f"files_{course_id}")  # missing fixture == hidden Files tab

    async def list_modules(self, course_id: int) -> list[dict[str, Any]] | None:
        return self._load(f"modules_{course_id}")

    async def get_self_id(self) -> int:
        return 7

    async def list_my_submissions(self, course_id: int) -> list[dict[str, Any]] | None:
        return self._load(f"submissions_{course_id}", [])

    async def list_conversations(self) -> list[dict[str, Any]] | None:
        return self._load("conversations", [])
