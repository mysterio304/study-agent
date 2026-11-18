"""Typed, endpoint-level Canvas access on top of the read-only `CanvasClient`.

`CanvasSource` is the protocol both the live API and the dry-run fixture fake implement.
Endpoints verified against https://canvas.instructure.com/doc/api/ (Nov 2026).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from agent.canvas.client import CanvasClient


@dataclass(frozen=True, slots=True)
class CanvasCourse:
    id: int
    name: str
    course_code: str | None
    current_score: float | None
    current_grade: str | None
    apply_group_weights: bool
    syllabus_html: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> CanvasCourse:
        student = next(
            (
                e
                for e in raw.get("enrollments") or []
                if e.get("type") in ("student", "StudentEnrollment")
            ),
            {},
        )
        return cls(
            id=int(raw["id"]),
            name=str(raw.get("name") or raw.get("course_code") or raw["id"]),
            course_code=raw.get("course_code"),
            current_score=student.get("computed_current_score"),
            current_grade=student.get("computed_current_grade"),
            apply_group_weights=bool(raw.get("apply_assignment_group_weights")),
            syllabus_html=raw.get("syllabus_body"),
        )


class CanvasSource(Protocol):
    async def list_courses(self) -> list[CanvasCourse]: ...
    async def get_course(self, course_id: int, *, with_syllabus: bool = True) -> CanvasCourse: ...
    async def list_assignments(self, course_id: int) -> list[dict[str, Any]]: ...
    async def list_assignment_groups(self, course_id: int) -> list[dict[str, Any]]: ...
    async def list_missing_submissions(self) -> list[dict[str, Any]]: ...
    async def list_announcements(
        self, course_ids: list[int], since: date
    ) -> list[dict[str, Any]]: ...
    async def list_files(self, course_id: int) -> list[dict[str, Any]] | None: ...
    async def list_modules(self, course_id: int) -> list[dict[str, Any]] | None: ...
    async def list_conversations(self) -> list[dict[str, Any]] | None: ...
    async def list_my_submissions(self, course_id: int) -> list[dict[str, Any]] | None: ...
    async def get_self_id(self) -> int: ...


class CanvasAPI:
    """Live implementation of `CanvasSource`."""

    def __init__(self, client: CanvasClient) -> None:
        self._c = client

    async def list_courses(self) -> list[CanvasCourse]:
        raw = await self._c.get_paginated(
            "/api/v1/courses",
            {"enrollment_state": "active", "include[]": ["total_scores", "term", "syllabus_body"]},
        )
        # Courses the user can no longer access come back with only `access_restricted_by_date`.
        return [CanvasCourse.from_api(c) for c in raw if "name" in c]

    async def get_course(self, course_id: int, *, with_syllabus: bool = True) -> CanvasCourse:
        include = ["total_scores"] + (["syllabus_body"] if with_syllabus else [])
        raw = await self._c.get_json(f"/api/v1/courses/{course_id}", {"include[]": include})
        return CanvasCourse.from_api(raw)

    async def list_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return await self._c.get_paginated(
            f"/api/v1/courses/{course_id}/assignments",
            {"include[]": ["submission", "score_statistics"], "order_by": "due_at"},
        )

    async def list_assignment_groups(self, course_id: int) -> list[dict[str, Any]]:
        return await self._c.get_paginated(
            f"/api/v1/courses/{course_id}/assignment_groups",
            {"include[]": ["assignments", "submission"]},
        )

    async def list_missing_submissions(self) -> list[dict[str, Any]]:
        return await self._c.get_paginated(
            "/api/v1/users/self/missing_submissions", {"filter[]": ["submittable"]}
        )

    async def list_announcements(self, course_ids: list[int], since: date) -> list[dict[str, Any]]:
        if not course_ids:
            return []
        return await self._c.get_paginated(
            "/api/v1/announcements",
            {
                "context_codes[]": [f"course_{cid}" for cid in course_ids],
                "start_date": since.isoformat(),  # default window is only 14 days
            },
        )

    async def list_files(self, course_id: int) -> list[dict[str, Any]] | None:
        """None when the Files tab is hidden — callers then fall back to module items."""
        return await self._c.optional(
            self._c.get_paginated(f"/api/v1/courses/{course_id}/files", {"sort": "updated_at"})
        )

    async def list_modules(self, course_id: int) -> list[dict[str, Any]] | None:
        return await self._c.optional(
            self._c.get_paginated(
                f"/api/v1/courses/{course_id}/modules", {"include[]": ["items", "content_details"]}
            )
        )

    async def list_conversations(self) -> list[dict[str, Any]] | None:
        # Default scope = all non-archived conversations (valid scopes: unread/starred/archived/sent).
        return await self._c.optional(self._c.get_paginated("/api/v1/conversations"))

    async def get_self_id(self) -> int:
        return int((await self._c.get_json("/api/v1/users/self"))["id"])

    async def list_my_submissions(self, course_id: int) -> list[dict[str, Any]] | None:
        """The caller's submissions for every assignment in a course, with grader comments."""
        return await self._c.optional(
            self._c.get_paginated(
                f"/api/v1/courses/{course_id}/students/submissions",
                {"include[]": ["submission_comments"]},
            )
        )
