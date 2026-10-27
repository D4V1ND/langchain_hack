import json
import threading

import pytest

import brain


def test_lists_only_grow():
    note = brain.upsert_note(None, {"name": "Area of a circle", "mistakes": ["Using d as r"]})
    brain.upsert_note(note["id"], {"mistakes": []})
    brain.upsert_note(note["id"], {"mistakes": ["Forgetting to square"]})
    brain.upsert_note(note["id"], {"mistakes": ["something else"], "change_reason": "rewrite"})
    got = brain.get_note(note["id"])
    assert "Using d as r" in got["mistakes"]  # an existing mistake can never be removed
    assert got["mistakes"] == ["Using d as r", "Forgetting to square", "something else"]


def test_duplicates_are_skipped_but_new_items_added():
    note = brain.upsert_note(None, {"name": "N", "insights": ["Area is 2D"]})
    again = brain.upsert_note(note["id"], {"insights": ["  area   is 2d ", "New point"]})
    assert again["insights"] == ["Area is 2D", "New point"]


def test_scalar_change_needs_reason():
    note = brain.upsert_note(None, {"name": "Circle area", "statement": "A = pi r^2", "when_to_use": "discs"})
    kept = brain.upsert_note(note["id"], {"name": "Renamed", "statement": "A = 2 pi r"})
    assert (kept["name"], kept["statement"]) == ("Circle area", "A = pi r^2")
    changed = brain.upsert_note(
        note["id"], {"statement": "A = \\pi r^2", "change_reason": "use LaTeX"}
    )
    assert changed["statement"] == "A = \\pi r^2"
    assert changed["id"] == note["id"]


def test_empty_values_never_blank_a_field():
    note = brain.upsert_note(None, {"name": "N", "statement": "S", "when_to_use": "W",
                                    "variables": [{"symbol": "r", "meaning": "radius"}]})
    out = brain.upsert_note(note["id"], {"statement": "", "when_to_use": "  ", "variables": [],
                                         "change_reason": "because"})
    assert out["statement"] == "S" and out["when_to_use"] == "W"
    assert out["variables"] == [{"symbol": "r", "meaning": "radius"}]


def test_empty_field_can_be_filled_without_reason():
    note = brain.upsert_note(None, {"name": "N"})
    out = brain.upsert_note(note["id"], {"statement": "S", "variables": [{"symbol": "x", "meaning": "m"}]})
    assert out["statement"] == "S" and out["variables"][0]["symbol"] == "x"


def test_same_name_without_id_updates_instead_of_duplicating():
    a = brain.upsert_note(None, {"name": "Area of a circle", "examples": ["e1"]})
    b = brain.upsert_note(None, {"name": "  area of a CIRCLE ", "examples": ["e2"]})
    assert a["id"] == b["id"]
    assert len(brain.list_notes()) == 1
    assert brain.get_note(a["id"])["examples"] == ["e1", "e2"]


def test_unknown_id_and_missing_name_raise():
    with pytest.raises(ValueError):
        brain.upsert_note("nope", {"name": "x"})
    with pytest.raises(ValueError):
        brain.upsert_note(None, {"name": "   "})
    with pytest.raises(ValueError):
        brain.get_note("nope")
    assert brain.list_notes() == []


def test_unchanged_upsert_does_not_touch_updated_at():
    note = brain.upsert_note(None, {"name": "N", "mistakes": ["m"]})
    again = brain.upsert_note(note["id"], {"mistakes": ["m"]})
    assert again["updated_at"] == note["updated_at"]


def test_corrupt_notes_file_is_kept_aside():
    brain.DATA_DIR.mkdir(parents=True)
    brain.NOTES_PATH.write_text("{not json", encoding="utf-8")
    note = brain.upsert_note(None, {"name": "Fresh"})
    assert [n["name"] for n in brain.list_notes()] == ["Fresh"]
    backups = list(brain.DATA_DIR.glob("notes.corrupt-*.json"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{not json"
    assert note["name"] == "Fresh"


def test_hand_edited_notes_get_defaults():
    brain.DATA_DIR.mkdir(parents=True)
    brain.NOTES_PATH.write_text(json.dumps([{"id": "a", "name": "Old"}]), encoding="utf-8")
    note = brain.get_note("a")
    assert note["mistakes"] == [] and note["variables"] == []
    brain.upsert_note("a", {"mistakes": ["m"]})
    assert brain.get_note("a")["mistakes"] == ["m"]


def test_parallel_upserts_lose_nothing():
    def work(i):
        brain.upsert_note(None, {"name": f"Concept {i}", "insights": [f"i{i}"]})

    threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(brain.list_notes()) == 20
    assert not list(brain.DATA_DIR.glob("*.tmp"))


def test_notebook_orders_newest_first_and_escapes():
    brain.upsert_note(None, {"name": "Older"})
    brain.upsert_note(None, {"name": "<script>alert(1)</script>", "statement": "x < y & z",
                             "insights": ['say "hi"']})
    brain.build_notebook()
    page = brain.NOTEBOOK_PATH.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert page.index("alert(1)") < page.index("Older")  # newest first
    assert 'id="q"' in page and "<details" in page and "2 Notes" in page


def test_notebook_when_empty():
    brain.build_notebook()
    page = brain.NOTEBOOK_PATH.read_text(encoding="utf-8")
    assert "0 Notes" in page and "No Notes yet" in page
