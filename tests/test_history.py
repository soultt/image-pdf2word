"""Tests for conversion history backends."""

import json

import pytest

from pdf2word.history import HistoryDB, MemoryHistory


@pytest.mark.parametrize(
    "factory", [HistoryDB, lambda path: MemoryHistory()]
)
def test_history_crud_and_search(factory, tmp_path):
    db = factory(tmp_path / "history.db")

    record_id = db.add_conversion("in.pdf", "out.docx", {"dpi": 200, "extract_figures": False})
    rows = db.list_conversions()
    assert len(rows) == 1
    assert rows[0]["id"] == record_id
    assert rows[0]["status"] == "running"
    assert rows[0]["finished_at"] is None

    db.finish_conversion(record_id, "success", 1234, None)
    row = db.get(record_id)
    assert row["status"] == "success"
    assert row["duration_ms"] == 1234
    assert json.loads(row["params_json"])["dpi"] == 200

    assert len(db.search("in.pdf")) == 1
    assert len(db.search("out.docx")) == 1
    assert len(db.search("", "failed")) == 0
    assert len(db.search("no-such-file")) == 0

    db.delete(record_id)
    assert db.list_conversions() == []

    db.add_conversion("a.pdf", "a.docx", {})
    db.clear()
    assert db.list_conversions() == []


def test_failed_record_keeps_error(tmp_path):
    db = HistoryDB(tmp_path / "history.db")
    record_id = db.add_conversion("in.pdf", "out.docx", {})
    db.finish_conversion(record_id, "failed", 10, "boom")
    row = db.get(record_id)
    assert row["status"] == "failed"
    assert row["error"] == "boom"
    assert len(db.search("boom")) == 1
