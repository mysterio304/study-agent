"""Endpoint layer: correct paths/params, hidden-tab fallbacks, course parsing."""

from datetime import date

import httpx
import pytest
import respx

from agent.canvas.api import CanvasAPI, CanvasCourse
from agent.canvas.client import CanvasClient

BASE = "https://canvas.example.edu"


@pytest.fixture
async def api():
    client = CanvasClient(BASE, "tok-123456", max_retries=0)
    yield CanvasAPI(client)
    await client.aclose()


@respx.mock
async def test_list_courses_parses_scores_and_skips_restricted(api: CanvasAPI) -> None:
    route = respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "name": "Networks",
                    "course_code": "CS3201",
                    "apply_assignment_group_weights": True,
                    "enrollments": [
                        {
                            "type": "student",
                            "computed_current_score": 80.5,
                            "computed_current_grade": "A-",
                        }
                    ],
                },
                {"id": 2, "access_restricted_by_date": True},
            ],
        )
    )
    courses = await api.list_courses()
    assert courses == [CanvasCourse(1, "Networks", "CS3201", 80.5, "A-", True, None)]
    params = route.calls.last.request.url.params
    assert params["enrollment_state"] == "active"
    assert set(params.get_list("include[]")) >= {"total_scores", "syllabus_body"}


@respx.mock
async def test_assignments_request_submission_and_stats(api: CanvasAPI) -> None:
    route = respx.get(f"{BASE}/api/v1/courses/7/assignments").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    assert await api.list_assignments(7) == [{"id": 1}]
    assert route.calls.last.request.url.params.get_list("include[]") == [
        "submission",
        "score_statistics",
    ]


@respx.mock
async def test_announcements_use_context_codes_and_start_date(api: CanvasAPI) -> None:
    route = respx.get(f"{BASE}/api/v1/announcements").mock(
        return_value=httpx.Response(200, json=[])
    )
    await api.list_announcements([1, 2], since=date(2026, 9, 1))
    params = route.calls.last.request.url.params
    assert params.get_list("context_codes[]") == ["course_1", "course_2"]
    assert params["start_date"] == "2026-09-01"
    assert await api.list_announcements([], since=date(2026, 9, 1)) == []


@respx.mock
async def test_hidden_files_tab_returns_none_and_modules_work(api: CanvasAPI) -> None:
    respx.get(f"{BASE}/api/v1/courses/9/files").mock(return_value=httpx.Response(403))
    respx.get(f"{BASE}/api/v1/courses/9/modules").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "Week 1", "items": []}])
    )
    assert await api.list_files(9) is None
    assert (await api.list_modules(9))[0]["name"] == "Week 1"


@respx.mock
async def test_other_endpoints(api: CanvasAPI) -> None:
    respx.get(f"{BASE}/api/v1/courses/3").mock(
        return_value=httpx.Response(
            200, json={"id": 3, "name": "Algebra", "syllabus_body": "<p>x</p>"}
        )
    )
    respx.get(f"{BASE}/api/v1/courses/3/assignment_groups").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "group_weight": 40}])
    )
    respx.get(f"{BASE}/api/v1/users/self/missing_submissions").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{BASE}/api/v1/conversations").mock(return_value=httpx.Response(401))
    assert (await api.get_course(3)).syllabus_html == "<p>x</p>"
    assert (await api.list_assignment_groups(3))[0]["group_weight"] == 40
    assert await api.list_missing_submissions() == []
    assert await api.list_conversations() is None
