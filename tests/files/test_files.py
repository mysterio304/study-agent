"""Extraction, chunking, FTS search and the indexer."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from agent.brain.budgets import RAG_CHUNK_BUDGET, estimate_tokens
from agent.canvas.api import FileUnavailable
from agent.core.clock import FixedClock
from agent.db.models import Chunk, Course, CourseFile, Exam
from agent.files.chunking import chunk_segments
from agent.files.extract import ExtractionError, Segment, extract
from agent.files.indexer import index_local_file, index_pending
from agent.files.search import FtsRetriever, fts_query
from agent.prep.seed import seed_exams
from tests.files.samples import make_docx, make_pdf, make_pptx
from tests.planner.conftest import HKT, at


def test_pdf_pages_have_locators(tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "l4.pdf", ["TCP congestion control uses cwnd", "", "Slow start doubles cwnd"]
    )
    segs = extract(pdf)
    assert [s.locator for s in segs] == ["p. 1", "p. 3"]  # empty page skipped
    assert "cwnd" in segs[0].text


def test_pptx_slides_include_notes(tmp_path: Path) -> None:
    pptx = make_pptx(
        tmp_path / "l7.pptx",
        [
            ("Routing", "Link state vs distance vector", "Dijkstra per router"),
            ("BGP", "Path vector protocol", None),
        ],
    )
    segs = extract(pptx)
    assert [s.locator for s in segs] == ["slide 1", "slide 2"]
    assert "Notes: Dijkstra per router" in segs[0].text


def test_docx_sections_follow_headings(tmp_path: Path) -> None:
    docx = make_docx(
        tmp_path / "notes.docx", [("Eigenvalues", "Av = λv"), ("Diagonalisation", "A = PDP^-1")]
    )
    segs = extract(docx)
    assert [s.locator for s in segs] == ["§1", "§2"] and "λ" in segs[0].text


def test_unsupported_and_corrupt_files(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError):
        extract(tmp_path / "x.zip")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    with pytest.raises(ExtractionError):
        extract(bad)


def test_chunking_splits_long_and_merges_tiny() -> None:
    long = Segment(
        "p. 5",
        ". ".join(f"Sentence number {i} about TCP retransmission timers" for i in range(120)),
    )
    tiny = [Segment(f"slide {i}", f"Short slide {i}") for i in range(1, 5)]
    chunks = chunk_segments([long, *tiny])
    assert all(estimate_tokens(c.text) <= RAG_CHUNK_BUDGET for c in chunks)
    assert chunks[0].locator == "p. 5" and chunks[1].locator == "p. 5 (2)"
    assert chunks[-1].locator == "slides 1–4"


def test_fts_query_is_injection_safe() -> None:
    assert fts_query('объясни TCP "cwnd" OR NEAR(x)') == '"tcp" OR "cwnd" OR "or" OR "near"'
    assert fts_query("что как") is None


@pytest.fixture
async def db(sessionmaker):
    clk = FixedClock(at(date(2026, 11, 19), 10), HKT)
    async with sessionmaker() as s:
        await seed_exams(s)
        s.add(
            Course(
                id=101,
                name="CS3201",
                apply_group_weights=True,
                content_hash="x",
                synced_at=clk.now(),
            )
        )
        s.add(
            Course(
                id=105, name="DB", apply_group_weights=True, content_hash="x", synced_at=clk.now()
            )
        )
        await s.flush()
        (await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))).course_id = 101
        await s.commit()
    return sessionmaker


async def test_index_and_search_with_citations(db, tmp_path) -> None:
    clk = FixedClock(at(date(2026, 11, 19), 10), HKT)
    pdf = make_pdf(
        tmp_path / "L5.pdf",
        [
            "TCP congestion control: cwnd halves on triple duplicate ACK",
            "Routing: Dijkstra computes shortest paths",
        ],
    )
    async with db() as s:
        row = CourseFile(
            course_id=101,
            display_name="Lecture 5.pdf",
            source="files_api",
            kind="lecture",
            index_state="pending",
        )
        s.add(row)
        await s.commit()
    result = await index_local_file(db, row.id, pdf, clk)
    assert result.state == "indexed" and result.chunks >= 1
    async with db() as s:
        hits = await FtsRetriever().search(
            s, "почему cwnd уменьшается при duplicate ACK", course_id=101
        )
        assert hits and hits[0].citation().startswith("Lecture 5.pdf, ") and "cwnd" in hits[0].text
        assert await FtsRetriever().search(s, "cwnd", course_id=999) == []
    # Re-indexing replaces chunks (no duplicates in the FTS index)
    await index_local_file(db, row.id, pdf, clk)
    async with db() as s:
        assert await s.scalar(select(func.count(Chunk.id))) == result.chunks
        hits = await FtsRetriever().search(s, "cwnd")
        assert len(hits) == 1


class Files:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.calls: list[int] = []

    async def download_file(self, file_id: int, dest: Path, *, max_bytes: int) -> int:
        self.calls.append(file_id)
        if file_id == 3:
            raise FileUnavailable("locked")
        dest.parent.mkdir(parents=True, exist_ok=True)
        make_pdf(dest, [f"content of file {file_id} about sockets"])
        return dest.stat().st_size


async def test_index_pending_only_exam_courses_and_supported_types(db, tmp_path) -> None:
    clk = FixedClock(at(date(2026, 11, 19), 10), HKT)
    async with db() as s:
        s.add_all(
            [
                CourseFile(
                    canvas_file_id=1,
                    course_id=101,
                    display_name="L1.pdf",
                    source="files_api",
                    kind="lecture",
                    index_state="pending",
                ),
                CourseFile(
                    canvas_file_id=2,
                    course_id=101,
                    display_name="video.mp4",
                    source="files_api",
                    kind="lecture",
                    index_state="pending",
                ),
                CourseFile(
                    canvas_file_id=3,
                    course_id=101,
                    display_name="locked.pdf",
                    source="files_api",
                    kind="lecture",
                    index_state="pending",
                ),
                CourseFile(
                    canvas_file_id=4,
                    course_id=105,
                    display_name="other.pdf",
                    source="files_api",
                    kind="lecture",
                    index_state="pending",
                ),
            ]
        )
        await s.commit()
    source = Files(tmp_path)
    results = await index_pending(db, source, tmp_path, clk)
    assert [r.name for r in results] == ["L1.pdf"] and source.calls == [1, 3]
    async with db() as s:
        states = {
            f.display_name: f.index_state for f in (await s.scalars(select(CourseFile))).all()
        }
    assert states == {
        "L1.pdf": "indexed",
        "video.mp4": "unsupported",
        "locked.pdf": "unavailable",
        "other.pdf": "pending",
    }
