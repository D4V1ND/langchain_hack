import io
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain  # noqa: E402


class ScriptedModel(BaseChatModel):
    """Plays back a fixed list of replies, one per model call.

    An item is an AIMessage, an Exception to raise, or a callable(messages) -> AIMessage.
    """

    script: list = []
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("scripted model ran out of replies")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        msg = item(messages) if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def explanation_args(name: str = "Area of a circle", **extra) -> dict:
    data = {
        "answer": "Area grows with length times length.",
        "name": name,
        "statement": "A = \\pi r^2",
        "variables": [{"symbol": "r", "meaning": "radius"}],
        "when_to_use": "Finding the area of a disc.",
        "why_here": "The worked solution needs the disc area.",
        "mistakes": ["Using the diameter as r."],
        "example": "r = 3 gives A = 9 pi.",
    }
    data.update(extra)
    return {"explanation": data}


def shown_turn(call_id: str, **extra) -> list:
    """Model replies for one tutor turn: show_explanation, then 'Done.'."""
    return [tool_call("show_explanation", explanation_args(**extra), call_id), AIMessage(content="Done.")]


def png_bytes(size=(64, 48), color=(250, 250, 250)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def isolated_brain(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(brain, "DATA_DIR", data)
    monkeypatch.setattr(brain, "SESSIONS_DIR", data / "sessions")
    monkeypatch.setattr(brain, "NOTES_PATH", data / "notes.json")
    monkeypatch.setattr(brain, "NOTEBOOK_PATH", data / "notebook.html")
    monkeypatch.setattr(brain, "_agent", None)
    brain._sessions.clear()
    brain._explanations.clear()
    brain.last_usage.clear()
    yield
    brain._sessions.clear()


@pytest.fixture
def use_model(monkeypatch):
    """Install a brain agent that runs on a ScriptedModel; returns the model."""

    def install(script: list) -> ScriptedModel:
        model = ScriptedModel(script=list(script), calls=[])
        monkeypatch.setattr(brain, "_agent", brain._build_agent(model))
        return model

    return install
