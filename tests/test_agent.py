import json
import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import brain
from conftest import explanation_args, png_bytes, shown_turn, tool_call


def image_count(messages):
    n = 0
    for m in messages:
        if isinstance(m, HumanMessage) and isinstance(m.content, list):
            n += sum(1 for b in m.content if b.get("type") == "image_url")
    return n


def human_messages(messages):
    return [m for m in messages if isinstance(m, HumanMessage)]


def consolidation_script(*, names=("Area of a circle", "Radius from diameter")):
    """Tutor delegates to the consolidator, which reads, lists and upserts."""
    steps = [
        tool_call("task", {"description": "Consolidate Session SID", "subagent_type": "consolidator"}, "t1"),
        lambda msgs: tool_call("read_transcript", {"session_id": brain._last_consolidating}, "r1"),
        tool_call("list_notes", {}, "l1"),
    ]
    for i, name in enumerate(names):
        steps.append(
            tool_call(
                "upsert_note",
                {"name": name, "statement": "S", "mistakes": [f"m{i}"], "examples": [f"e{i}"]},
                f"u{i}",
            )
        )
    steps += [AIMessage(content=f"Saved {len(names)} Notes."), AIMessage(content="Consolidated.")]
    return steps


@pytest.fixture(autouse=True)
def track_consolidating(monkeypatch):
    """Let scripted replies know which Session id is being consolidated."""
    original = brain._consolidate_transcript

    def wrapper(session_id):
        brain._last_consolidating = session_id
        return original(session_id)

    monkeypatch.setattr(brain, "_consolidate_transcript", wrapper)


def test_first_question_carries_images_follow_up_does_not(use_model):
    model = use_model(shown_turn("c1") + shown_turn("c2", name="Radius from diameter"))
    sid = brain.start_session(png_bytes((3000, 2000)), png_bytes((40, 40)))
    first = brain.ask(sid, "Why is r squared?")
    assert first.name == "Area of a circle" and first.variables[0].symbol == "r"
    brain.ask(sid, "What if I'm given the diameter?")

    first_call, second_call = model.calls[0], model.calls[2]
    assert image_count(human_messages(first_call)) == 2
    # the second question is text only, but the images are still in the history
    last_human = human_messages(second_call)[-1]
    assert isinstance(last_human.content, str)
    assert image_count(human_messages(second_call)) == 2


def test_images_are_resized_and_use_one_detail(use_model):
    model = use_model(shown_turn("c1"))
    sid = brain.start_session(png_bytes((4000, 3000)), png_bytes((2000, 2000)))
    brain.ask(sid, "q")
    blocks = [b for m in human_messages(model.calls[0]) for b in m.content if b.get("type") == "image_url"]
    assert {b["image_url"]["detail"] for b in blocks} == {brain.IMAGE_DETAIL}
    import base64, io
    from PIL import Image
    sizes = [
        Image.open(io.BytesIO(base64.b64decode(b["image_url"]["url"].split(",", 1)[1]))).size
        for b in blocks
    ]
    assert max(sizes[0]) <= brain.SNAPSHOT_MAX_SIDE and max(sizes[1]) <= brain.CROP_MAX_SIDE


def test_bad_images_are_rejected():
    with pytest.raises(ValueError):
        brain.start_session(b"", png_bytes())
    with pytest.raises(ValueError):
        brain.start_session(b"not a png", png_bytes())


def test_system_prompt_is_identical_every_call(use_model):
    model = use_model(shown_turn("c1") + shown_turn("c2"))
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "one")
    brain.ask(sid, "two")
    systems = [str(c[0].content) for c in model.calls]
    assert len(set(systems)) == 1
    assert sid not in systems[0]  # no Session id in the cached prefix


def test_fallback_to_text_reply_when_tool_not_called(use_model):
    use_model([AIMessage(content="Which part do you mean?")])
    sid = brain.start_session(png_bytes(), png_bytes())
    out = brain.ask(sid, "what?")
    assert out.answer == "Which part do you mean?" and out.name == ""


def test_last_show_explanation_wins(use_model):
    use_model(
        [
            tool_call("show_explanation", explanation_args("First"), "c1"),
            tool_call("show_explanation", explanation_args("Second"), "c2"),
            AIMessage(content="Done."),
        ]
    )
    sid = brain.start_session(png_bytes(), png_bytes())
    assert brain.ask(sid, "q").name == "Second"


def test_blank_answer_in_tool_falls_back_to_reply(use_model):
    use_model(
        [
            tool_call("show_explanation", {"explanation": {"answer": "  ", "name": "X"}}, "c1"),
            AIMessage(content="Because."),
        ]
    )
    sid = brain.start_session(png_bytes(), png_bytes())
    assert brain.ask(sid, "q").answer == "Because."


def test_empty_reply_raises_and_leaves_clean_state(use_model):
    model = use_model([AIMessage(content=""), *shown_turn("c1")])
    sid = brain.start_session(png_bytes(), png_bytes())
    with pytest.raises(RuntimeError):
        brain.ask(sid, "q")
    assert brain.ask(sid, "q again").name == "Area of a circle"
    retry_humans = human_messages(model.calls[-2])  # first model call of the retry
    assert len(retry_humans) == 1 and image_count(retry_humans) == 2


def test_api_error_rolls_back_and_retry_resends_images(use_model):
    model = use_model([RuntimeError("boom"), *shown_turn("c1")])
    sid = brain.start_session(png_bytes(), png_bytes())
    with pytest.raises(RuntimeError, match="boom"):
        brain.ask(sid, "q")
    out = brain.ask(sid, "q")
    assert out.name == "Area of a circle"
    retry_call = model.calls[1]
    assert len(human_messages(retry_call)) == 1  # the failed message is gone
    assert image_count(human_messages(retry_call)) == 2  # and the images were re-sent


def test_api_error_on_follow_up_keeps_earlier_turns(use_model):
    model = use_model([*shown_turn("c1"), RuntimeError("boom"), *shown_turn("c2")])
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "one")
    with pytest.raises(RuntimeError):
        brain.ask(sid, "two")
    brain.ask(sid, "two")
    humans = human_messages(model.calls[-2])
    assert len(humans) == 2  # question one and the retried question two, no duplicate
    assert image_count(humans) == 2


def test_ask_validates_input(use_model):
    use_model([])
    sid = brain.start_session(png_bytes(), png_bytes())
    with pytest.raises(ValueError):
        brain.ask(sid, "   ")
    with pytest.raises(KeyError):
        brain.ask("nope", "q")


def test_missing_api_key_gives_clear_error(monkeypatch):
    monkeypatch.setattr(brain, "MODEL", "openai:gpt-5.1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sid = brain.start_session(png_bytes(), png_bytes())
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        brain.ask(sid, "q")


def test_end_session_saves_transcript_even_when_consolidation_fails(use_model):
    use_model([*shown_turn("c1"), RuntimeError("network off")])
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "Why is r squared?")
    with pytest.raises(RuntimeError, match="network off"):
        brain.end_session(sid)
    t = json.loads((brain.SESSIONS_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    assert t["consolidated"] is False
    assert t["turns"][0]["question"] == "Why is r squared?"
    assert t["turns"][0]["explanation"]["name"] == "Area of a circle"
    assert "data:image" not in json.dumps(t)  # the Snapshot is never saved


def test_full_flow_two_notes_and_restart_retry(use_model, monkeypatch):
    # Session: two turns about two Concepts. Network dies during Consolidation.
    use_model([*shown_turn("c1"), *shown_turn("c2", name="Radius from diameter"), RuntimeError("down")])
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "Why is r squared?")
    brain.ask(sid, "What if I'm given the diameter?")
    with pytest.raises(RuntimeError):
        brain.end_session(sid)
    assert brain.list_notes() == []

    # "Restart": network is back, the startup retry finds the Transcript.
    use_model(consolidation_script())
    assert brain.consolidate_pending() == 2
    assert {n["name"] for n in brain.list_notes()} == {"Area of a circle", "Radius from diameter"}
    page = brain.NOTEBOOK_PATH.read_text(encoding="utf-8")
    assert "Area of a circle" in page and "Radius from diameter" in page
    t = json.loads((brain.SESSIONS_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    assert t["consolidated"] is True

    # Nothing left to do, and no second agent run is needed.
    use_model([])
    assert brain.consolidate_pending() == 0


def test_end_session_returns_note_count(use_model):
    use_model([*shown_turn("c1"), *consolidation_script(names=("Area of a circle",))])
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "q")
    assert brain.end_session(sid) == 1
    assert [n["name"] for n in brain.list_notes()] == ["Area of a circle"]


def test_second_session_enriches_instead_of_duplicating(use_model):
    use_model([*shown_turn("c1"), *consolidation_script(names=("Area of a circle",))])
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "q")
    brain.end_session(sid)

    use_model([*shown_turn("c2"), *consolidation_script(names=("area of a circle",))])
    sid2 = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid2, "q")
    # same name, but the existing Note gets nothing new except nothing: m0/e0 are duplicates
    assert brain.end_session(sid2) == 0
    notes = brain.list_notes()
    assert len(notes) == 1
    assert brain.get_note(notes[0]["id"])["mistakes"] == ["m0"]


def test_consolidator_that_saves_nothing_is_a_failure(use_model):
    use_model(
        [
            *shown_turn("c1"),
            tool_call("task", {"description": "go", "subagent_type": "consolidator"}, "t1"),
            AIMessage(content="I did nothing."),
            AIMessage(content="All done."),
        ]
    )
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "q")
    with pytest.raises(RuntimeError, match="never read"):
        brain.end_session(sid)
    t = json.loads((brain.SESSIONS_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    assert t["consolidated"] is False


def test_session_with_only_clarifying_questions_skips_consolidation(use_model):
    use_model([AIMessage(content="Which part?")])
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "what?")
    assert brain.end_session(sid) == 0  # no model call is scripted, so none may happen
    t = json.loads((brain.SESSIONS_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    assert t["consolidated"] is True


def test_session_with_no_questions(use_model):
    use_model([])
    sid = brain.start_session(png_bytes(), png_bytes())
    assert brain.end_session(sid) == 0
    assert (brain.SESSIONS_DIR / f"{sid}.json").exists()


def test_end_session_twice_and_unknown(use_model):
    use_model([*shown_turn("c1"), *consolidation_script(names=("Area of a circle",))])
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "q")
    assert brain.end_session(sid) == 1
    assert brain.end_session(sid) == 0  # already consolidated
    with pytest.raises(KeyError):
        brain.end_session("2000-01-01T00-00-00")
    with pytest.raises(ValueError):
        brain.end_session("../../etc/passwd")
    with pytest.raises(KeyError):
        brain.ask(sid, "late question")


def test_session_ids_are_unique_within_one_second():
    ids = {brain.start_session(png_bytes(), png_bytes()) for _ in range(5)}
    assert len(ids) == 5


def test_read_transcript_rejects_bad_ids():
    assert brain._tool_read_transcript("../notes").startswith("ERROR")
    assert brain._tool_read_transcript("2000-01-01T00-00-00").startswith("ERROR")


def test_consolidate_pending_with_nothing_builds_notebook():
    assert brain.consolidate_pending() == 0
    assert brain.NOTEBOOK_PATH.exists()


def test_consolidate_pending_continues_past_a_bad_transcript(use_model):
    brain.SESSIONS_DIR.mkdir(parents=True)
    (brain.SESSIONS_DIR / "2000-01-01T00-00-00.json").write_text("{broken", encoding="utf-8")
    good = {"id": "2000-01-02T00-00-00", "consolidated": False,
            "turns": [{"question": "q", "explanation": {"answer": "a", "name": "Area of a circle"}}]}
    (brain.SESSIONS_DIR / "2000-01-02T00-00-00.json").write_text(json.dumps(good), encoding="utf-8")
    use_model(consolidation_script(names=("Area of a circle",)))
    with pytest.raises(RuntimeError, match="1 Transcript"):
        brain.consolidate_pending()
    assert [n["name"] for n in brain.list_notes()] == ["Area of a circle"]


def test_concurrent_end_and_pending_consolidate_once(use_model):
    use_model([*shown_turn("c1"), *consolidation_script(names=("Area of a circle",))])
    sid = brain.start_session(png_bytes(), png_bytes())
    brain.ask(sid, "q")
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(brain.end_session(sid))),
    ]
    threads[0].start()
    threads[0].join()
    # the startup retry racing a finished end_session must not run the agent again
    use_model([])
    assert brain.consolidate_pending() == 0
    assert results == [1]
