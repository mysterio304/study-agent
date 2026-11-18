from sqlalchemy import select

from agent.canvas.fake import FakeCanvasSource
from agent.canvas.sync import sync_courses
from agent.db.models import Course, SyncState


async def test_sync_courses_upserts_and_reports_changes(sessionmaker, clock) -> None:
    source = FakeCanvasSource()
    async with sessionmaker() as s:
        changed = await sync_courses(s, source, clock)
        await s.commit()
    assert len(changed) == 6

    async with sessionmaker() as s:
        courses = (await s.scalars(select(Course).order_by(Course.id))).all()
        state = await s.get(SyncState, "courses")
    assert courses[0].name.startswith("CS3201")
    assert courses[0].current_score == 78.4
    assert "Final examination: 70%" in (courses[0].syllabus_html or "")
    assert state is not None and state.initialized

    # A second sync with identical data reports no changes.
    async with sessionmaker() as s:
        assert await sync_courses(s, source, clock) == []
