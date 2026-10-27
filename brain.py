"""StudyLens brain: tutor agent, Transcripts, Notes and the Notebook.

The app talks to this module only through start_session, ask, end_session and
consolidate_pending (SPEC.md section 4), and reads NOTEBOOK_PATH. It never
touches data/ and this module never imports Qt.

Run it alone on a screenshot:
    python brain.py test.png "Why is r squared?" ["follow-up" ...] [--end]
"""
import base64
import copy
import html
import io
import json
import logging
import os
import queue
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, tool
from PIL import Image
from pydantic import BaseModel, Field

load_dotenv()

log = logging.getLogger("studylens.brain")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# One constant for the image-reading model. STUDYLENS_MODEL overrides it.
MODEL = os.getenv("STUDYLENS_MODEL", "openai:gpt-5.1")
# Live speech-to-text for spoken questions: OpenAI's recommended realtime transcription model.
LIVE_TRANSCRIBE_MODEL = os.getenv("STUDYLENS_LIVE_TRANSCRIBE_MODEL", "gpt-live-transcribe")
LIVE_AUDIO_RATE = 24000  # the app streams 24 kHz mono 16-bit PCM

# The same `detail` on every image keeps the cached prompt prefix identical.
IMAGE_DETAIL = "high"
SNAPSHOT_MAX_SIDE = 1600
CROP_MAX_SIDE = 1024

DATA_DIR = Path(__file__).resolve().parent / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
NOTES_PATH = DATA_DIR / "notes.json"
NOTEBOOK_PATH = DATA_DIR / "notebook.html"  # the tray menu opens this


# ---------------------------------------------------------------------------
# Contract types (SPEC.md section 4)
# ---------------------------------------------------------------------------


class Variable(BaseModel):
    symbol: str
    meaning: str


class Explanation(BaseModel):
    answer: str  # always filled; a clarifying question if the Selection is unclear
    name: str = ""
    statement: str = ""  # LaTeX or a plain sentence
    variables: list[Variable] = []
    when_to_use: str = ""
    why_here: str = ""
    mistakes: list[str] = []
    example: str = ""
    html: str = ""  # the panel page the model designs for this answer; shown with scripts off


# ---------------------------------------------------------------------------
# Prompts. Both are static on purpose: no time, Session id or notes list in
# them, so the cached prompt prefix never changes.
# ---------------------------------------------------------------------------

TUTOR_PROMPT = """\
You are StudyLens, a patient tutor for maths and science students.

A student has frozen their screen (a Snapshot) and drawn a shape (a Selection) \
around something that confuses them. Your first message in a Session contains the \
Snapshot with the Selection drawn in red, a zoomed crop of the Selection, and the \
student's first question. Later messages are text-only follow-up questions; the \
images are still part of this conversation, so keep using what you saw.

For every question, call the `show_explanation` tool exactly once, then reply with \
the single word "Done." Never write the explanation in your reply; the student only \
sees what you put in `show_explanation`.

Fill the Explanation for the ONE Concept the latest question is about:
- answer: a short, direct answer to the latest question, in plain sentences.
- name: the Concept's name, e.g. "Area of a circle".
- statement: the Concept as LaTeX without $ delimiters (e.g. A = \\pi r^2), or one \
plain sentence if it is a rule or method.
- variables: each symbol with its meaning. Leave empty if there are none.
- when_to_use: when this Concept applies.
- why_here: why it appears in what the student selected.
- mistakes: common mistakes with this Concept.
- example: one short worked example.
- html: the page the student sees on a cream card in a 430 px wide panel, designed \
fresh for this answer. Write an HTML fragment (no <html>, <head> or <body>). Lead \
with the short answer in <div class="answer">, then teach the Concept in whatever \
layout works best. The panel has a hand-drawn style and already styles h2, h3, \
lists and tables, plus these classes: "answer" (the short answer), "formula" (a big \
centred formula), "box" (a highlighted idea) and "warn" (a common mistake). Use them \
instead of your own fonts, borders or colours. Draw small diagrams with inline <svg> \
using only these colours: ink #26262b (2px strokes), paper #f6f1e4, teal #6cc3bc, \
mustard #f2b632, coral #e2733f, sage #a8c6a6. Write maths with MathML (<math>) or \
<sup>/<sub>, never LaTeX: nothing renders LaTeX in the panel. Scripts, links and \
external images or fonts are blocked, so don't use them. Keep it under about 300 words.
Use `answer` and a short `html` alone when you cannot fill the rest.

If the Selection is unreadable or ambiguous, fill in only `answer` and `html`, with \
one clarifying question. Never guess what the student meant.

Be accurate. If you are unsure of a step, say so in `answer`.

Consolidation: when a message starts with "Consolidate Session", hand the job to \
the `consolidator` sub-agent with the `task` tool, passing the Session id from the \
message. Do not answer it yourself and do not call `show_explanation`. When it \
finishes, reply with one short line.
"""

CONSOLIDATOR_PROMPT = """\
You turn one finished tutoring Session into revision Notes. A Note holds exactly \
one Concept (a formula, rule, theorem or method), and a Concept has at most one Note.

The Session id is in your task description. Work in this order:
1. Call `read_transcript` with the Session id. It lists every question and Explanation.
2. Call `list_notes` to see every existing Note's id, name and statement.
3. For each distinct Concept in the Transcript (group Explanations by `name`; \
"πr^2" and "\\pi r^{2}" are the same Concept):
   - If an existing Note is the same Concept, call `get_note` for it, then call \
`upsert_note` with its `note_id` and only the new material.
   - Otherwise call `upsert_note` with no `note_id` to create it.
4. Fill the fields from the Transcript:
   - name, statement, variables, when_to_use come from the Explanation.
   - Every `why_here` becomes one entry in `examples` (add the worked example if \
there is one).
   - Every item in `mistakes` goes into `mistakes`.
   - Key points the student learned from follow-up questions go into `insights`, \
one short sentence each.
5. The lists `mistakes`, `examples` and `insights` can only grow; nothing you \
send ever removes an entry. Change an existing Note's name, statement, variables \
or when_to_use only when you have a real reason, and state it in `change_reason`.
6. If a tool returns an error, read it and fix your call. Do not skip a Concept.

Finish with one line: how many Notes you saved.
"""

CONSOLIDATOR_DESCRIPTION = (
    "Turns a finished Session's Transcript into revision Notes: creates a Note for "
    "each new Concept and enriches the Note of each known one. Use it only when "
    "asked to consolidate a Session, and pass the Session id."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _norm(text: Any) -> str:
    return " ".join(str(text).split()).casefold()


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _check_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not _ID_RE.match(session_id):
        raise ValueError(f"bad session id: {session_id!r}")
    return session_id


# ---------------------------------------------------------------------------
# Notes store (data/notes.json)
# ---------------------------------------------------------------------------

_notes_lock = threading.RLock()
_NOTE_SCALARS = ("name", "statement", "when_to_use")
_NOTE_LISTS = ("mistakes", "examples", "insights")


def _clean_variables(raw: Any) -> list[dict]:
    out = []
    for item in raw or []:
        if isinstance(item, Variable):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        meaning = str(item.get("meaning") or "").strip()
        if symbol:
            out.append({"symbol": symbol, "meaning": meaning})
    return out


def _normalize_note(raw: dict) -> dict:
    """Fill defaults so a hand-edited or older notes.json can't crash us."""
    note = {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
        "name": str(raw.get("name") or ""),
        "statement": str(raw.get("statement") or ""),
        "variables": _clean_variables(raw.get("variables")),
        "when_to_use": str(raw.get("when_to_use") or ""),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
    }
    for key in _NOTE_LISTS:
        note[key] = [str(x) for x in (raw.get(key) or []) if str(x).strip()]
    return note


def _load_notes() -> list[dict]:
    with _notes_lock:
        try:
            raw = NOTES_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("notes.json is not a list")
            return [_normalize_note(n) for n in data if isinstance(n, dict)]
        except ValueError:
            # Never overwrite a file we can't read: keep it aside and start empty.
            backup = NOTES_PATH.with_name(
                f"notes.corrupt-{datetime.now():%Y%m%dT%H%M%S}.json"
            )
            os.replace(NOTES_PATH, backup)
            log.error("notes.json was unreadable; moved to %s", backup)
            return []


def _save_notes(notes: list[dict]) -> None:
    _atomic_write(NOTES_PATH, json.dumps(notes, indent=2, ensure_ascii=False))


def list_notes() -> list[dict]:
    """Every Note's id, name and statement."""
    return [
        {"id": n["id"], "name": n["name"], "statement": n["statement"]}
        for n in _load_notes()
    ]


def get_note(note_id: str) -> dict:
    for note in _load_notes():
        if note["id"] == note_id:
            return note
    raise ValueError(f"no Note with id {note_id!r}")


def upsert_note(note_id: Optional[str], fields: dict) -> dict:
    """Create a Note, or add to an existing one. Returns the saved Note.

    The rules live here, not in the prompt:
    - `id` never changes.
    - `mistakes`, `examples` and `insights` only grow (exact duplicates are skipped).
    - `name`, `statement`, `variables` and `when_to_use` of an existing Note change
      only when `fields["change_reason"]` is given. Empty values never overwrite.
    - Without a `note_id`, a Note with the same name is updated instead of duplicated.
    """
    fields = fields or {}
    with _notes_lock:
        notes = _load_notes()
        note = None
        if note_id:
            note = next((n for n in notes if n["id"] == note_id), None)
            if note is None:
                raise ValueError(f"no Note with id {note_id!r}")
        else:
            name = str(fields.get("name") or "").strip()
            if not name:
                raise ValueError("a new Note needs a name")
            note = next((n for n in notes if _norm(n["name"]) == _norm(name)), None)

        created = note is None
        if created:
            note = _normalize_note({"id": uuid.uuid4().hex[:12], "created_at": _now()})
        reason = str(fields.get("change_reason") or "").strip()
        changed = created

        for key in _NOTE_SCALARS:
            new = str(fields.get(key) or "").strip()
            if not new or new == note[key]:
                continue
            if note[key] and not reason:
                continue  # an existing value changes only with a stated reason
            note[key] = new
            changed = True

        new_vars = _clean_variables(fields.get("variables"))
        if new_vars and new_vars != note["variables"]:
            if not note["variables"] or reason:
                note["variables"] = new_vars
                changed = True

        for key in _NOTE_LISTS:
            seen = {_norm(x) for x in note[key]}
            for item in fields.get(key) or []:
                text = str(item).strip()
                if text and _norm(text) not in seen:
                    note[key].append(text)
                    seen.add(_norm(text))
                    changed = True

        if created and not note["name"]:
            raise ValueError("a new Note needs a name")
        if changed:
            note["updated_at"] = _now()
            if created:
                notes.append(note)
            _save_notes(notes)
        return copy.deepcopy(note)


# ---------------------------------------------------------------------------
# Notebook (data/notebook.html, rebuilt from notes.json)
# ---------------------------------------------------------------------------

_NOTEBOOK_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StudyLens Notebook</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1c2230; --muted:#667085; --line:#e2e5ea; --accent:#2f5bea; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14171f; --card:#1d212c; --ink:#e8ebf2; --muted:#9aa3b5; --line:#2c3242; --accent:#7b9bff; }
  }
  body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 system-ui, sans-serif; }
  main { max-width:760px; margin:0 auto; padding:24px 16px 64px; }
  h1 { margin:0 0 4px; font-size:1.6rem; }
  .sub { color:var(--muted); margin:0 0 16px; }
  input[type=search] { width:100%; box-sizing:border-box; padding:10px 12px; font:inherit;
    color:var(--ink); background:var(--card); border:1px solid var(--line); border-radius:8px; }
  details.note { background:var(--card); border:1px solid var(--line); border-radius:10px; margin:12px 0; }
  details.note[hidden] { display:none; }
  summary { cursor:pointer; padding:12px 14px; font-weight:600; }
  summary .when { float:right; color:var(--muted); font-weight:400; font-size:.85rem; }
  .body { padding:0 14px 14px; }
  .body h3 { margin:14px 0 4px; font-size:.8rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }
  .statement { font-size:1.15rem; padding:6px 0; overflow-x:auto; }
  ul { margin:4px 0; padding-left:20px; }
  #empty { color:var(--muted); text-align:center; margin-top:40px; }
</style>
</head>
<body>
<main>
  <h1>Notebook</h1>
  <p class="sub">__COUNT__</p>
  <input type="search" id="q" placeholder="Search Notes" autofocus>
  <div id="notes">
__NOTES__
  </div>
  <p id="empty" hidden>No Notes match.</p>
</main>
<script>
  var DELIMS = [
    {left:"$$", right:"$$", display:true}, {left:"\\\\[", right:"\\\\]", display:true},
    {left:"\\\\(", right:"\\\\)", display:false}, {left:"$", right:"$", display:false}
  ];
  function typeset() {
    if (!window.katex) return;
    document.querySelectorAll(".statement").forEach(function (el) {
      var t = el.textContent.trim()
        .replace(/^\\$\\$([\\s\\S]*)\\$\\$$/, "$1").replace(/^\\$([\\s\\S]*)\\$$/, "$1")
        .replace(/^\\\\\\(([\\s\\S]*)\\\\\\)$/, "$1").replace(/^\\\\\\[([\\s\\S]*)\\\\\\]$/, "$1");
      if (/[\\\\^_=]/.test(t)) katex.render(t, el, {throwOnError:false, displayMode:true});
    });
    if (window.renderMathInElement) {
      renderMathInElement(document.body, {delimiters:DELIMS, throwOnError:false, ignoredClasses:["statement"]});
    }
  }
  window.addEventListener("load", typeset);
  document.getElementById("q").addEventListener("input", function (e) {
    var q = e.target.value.trim().toLowerCase(), shown = 0;
    document.querySelectorAll("details.note").forEach(function (n) {
      var hit = !q || n.dataset.search.indexOf(q) !== -1;
      n.hidden = !hit;
      if (hit) shown++;
    });
    document.getElementById("empty").hidden = shown !== 0;
  });
</script>
</body>
</html>
"""


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _render_list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{_esc(i)}</li>" for i in items)
    return f"<h3>{title}</h3><ul>{lis}</ul>"


def _render_note(note: dict) -> str:
    parts = []
    if note["statement"]:
        parts.append(f'<div class="statement">{_esc(note["statement"])}</div>')
    if note["variables"]:
        lis = "".join(
            f"<li>\\({_esc(v['symbol'])}\\): {_esc(v['meaning'])}</li>"
            for v in note["variables"]
        )
        parts.append(f"<h3>Variables</h3><ul>{lis}</ul>")
    if note["when_to_use"]:
        parts.append(f"<h3>When to use</h3><p>{_esc(note['when_to_use'])}</p>")
    parts.append(_render_list("Mistakes", note["mistakes"]))
    parts.append(_render_list("Examples", note["examples"]))
    parts.append(_render_list("Insights", note["insights"]))
    haystack = " ".join(
        [note["name"], note["statement"], note["when_to_use"]]
        + [f"{v['symbol']} {v['meaning']}" for v in note["variables"]]
        + note["mistakes"]
        + note["examples"]
        + note["insights"]
    ).lower()
    when = note["updated_at"][:10]
    return (
        f'<details class="note" data-search="{_esc(haystack)}">'
        f'<summary>{_esc(note["name"])}<span class="when">{_esc(when)}</span></summary>'
        f'<div class="body">{"".join(parts)}</div></details>'
    )


def build_notebook() -> None:
    """Rebuild data/notebook.html from notes.json, newest first."""
    with _notes_lock:
        notes = sorted(_load_notes(), key=lambda n: n["updated_at"], reverse=True)
        count = f"{len(notes)} Note{'s' if len(notes) != 1 else ''}"
        body = "\n".join(_render_note(n) for n in notes)
        if not notes:
            body = '<p id="empty-start" style="color:var(--muted)">No Notes yet. Draw around something on your screen and ask about it.</p>'
        page = _NOTEBOOK_TEMPLATE.replace("__COUNT__", count).replace("__NOTES__", body)
        _atomic_write(NOTEBOOK_PATH, page)


def _build_notebook_quietly() -> None:
    try:
        build_notebook()
    except Exception:
        log.exception("could not rebuild the Notebook")


# ---------------------------------------------------------------------------
# Transcripts (data/sessions/<time>.json)
# ---------------------------------------------------------------------------


def _transcript_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{_check_id(session_id)}.json"


def _write_transcript(transcript: dict) -> None:
    _atomic_write(
        _transcript_path(transcript["id"]),
        json.dumps(transcript, indent=2, ensure_ascii=False),
    )


def _read_transcript(session_id: str) -> dict:
    return json.loads(_transcript_path(session_id).read_text(encoding="utf-8"))


def _has_concepts(transcript: dict) -> bool:
    return any(
        (t.get("explanation") or {}).get("name", "").strip()
        for t in transcript.get("turns", [])
    )


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

_explanations: dict[str, list[Explanation]] = {}
_explanations_lock = threading.Lock()

# What the consolidator did during the current Consolidation run; used to
# reject a run where it never read the Transcript or never saved anything.
_run: dict[str, Any] = {"read": set(), "upserts": 0}


@tool
def show_explanation(explanation: Explanation, config: RunnableConfig) -> str:
    """Show the Explanation for the latest question in the student's panel.
    Call it exactly once per question."""
    thread_id = (config.get("configurable") or {}).get("thread_id", "")
    with _explanations_lock:
        _explanations.setdefault(thread_id, []).append(explanation)
    return "Shown."


def _tool_read_transcript(session_id: str) -> str:
    try:
        transcript = _read_transcript(session_id)
    except FileNotFoundError:
        return f"ERROR: no Transcript for Session {session_id!r}"
    except ValueError as exc:
        return f"ERROR: {exc}"
    _run["read"].add(session_id)
    turns = [
        {"question": t.get("question", ""), "explanation": t.get("explanation", {})}
        for t in transcript.get("turns", [])
    ]
    return json.dumps({"session": session_id, "turns": turns}, ensure_ascii=False)


def _tool_list_notes() -> str:
    return json.dumps(list_notes(), ensure_ascii=False)


def _tool_get_note(note_id: str) -> str:
    try:
        return json.dumps(get_note(note_id), ensure_ascii=False)
    except ValueError as exc:
        return f"ERROR: {exc}"


class UpsertNoteArgs(BaseModel):
    note_id: Optional[str] = Field(
        default=None,
        description="Id of the existing Note to add to. Omit to create a new Note.",
    )
    name: str = ""
    statement: str = Field(default="", description="LaTeX or one plain sentence.")
    variables: list[Variable] = []
    when_to_use: str = ""
    mistakes: list[str] = Field(default=[], description="Entries to add; never removes.")
    examples: list[str] = Field(default=[], description="Entries to add; never removes.")
    insights: list[str] = Field(default=[], description="Entries to add; never removes.")
    change_reason: str = Field(
        default="",
        description="Required to change an existing Note's name, statement, "
        "variables or when_to_use.",
    )


def _tool_upsert_note(note_id: Optional[str] = None, **fields: Any) -> str:
    _run["upserts"] += 1
    try:
        note = upsert_note(note_id, fields)
    except ValueError as exc:
        return f"ERROR: {exc}"
    return json.dumps(
        {"saved": True, "id": note["id"], "name": note["name"]}, ensure_ascii=False
    )


def _consolidator_tools() -> list:
    return [
        StructuredTool.from_function(
            _tool_read_transcript,
            name="read_transcript",
            description="Return the questions and Explanations of a Session.",
        ),
        StructuredTool.from_function(
            _tool_list_notes,
            name="list_notes",
            description="Return every Note's id, name and statement.",
        ),
        StructuredTool.from_function(
            _tool_get_note,
            name="get_note",
            description="Return one full Note by id.",
        ),
        StructuredTool.from_function(
            _tool_upsert_note,
            name="upsert_note",
            description=(
                "Create a Note (omit note_id) or add to an existing one. The lists "
                "mistakes, examples and insights only grow."
            ),
            args_schema=UpsertNoteArgs,
        ),
    ]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

_agent = None
_agent_lock = threading.Lock()


def _build_agent(model: Any):
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    consolidator = {
        "name": "consolidator",
        "description": CONSOLIDATOR_DESCRIPTION,
        "system_prompt": CONSOLIDATOR_PROMPT,
        "tools": _consolidator_tools(),
    }
    return create_deep_agent(
        model=model,
        tools=[show_explanation],
        system_prompt=TUTOR_PROMPT,
        subagents=[consolidator],
        checkpointer=InMemorySaver(),
    )


def _get_agent():
    global _agent
    with _agent_lock:
        if _agent is None:
            if MODEL.startswith("openai:") and not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError(
                    "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
                )
            _agent = _build_agent(MODEL)
        return _agent


def _drop_thread(thread_id: str) -> None:
    """Free a finished thread's checkpoints."""
    try:
        _get_agent().checkpointer.delete_thread(thread_id)
    except Exception:
        log.debug("could not delete thread %s", thread_id, exc_info=True)


def _messages(agent, config: dict) -> list:
    return list(agent.get_state(config).values.get("messages", []))


def _rollback(agent, config: dict, keep: int) -> None:
    """Remove the messages a failed turn left behind, so a retry starts clean."""
    try:
        extra = _messages(agent, config)[keep:]
        removals = [RemoveMessage(id=m.id) for m in extra if m.id]
        if removals:
            agent.update_state(config, {"messages": removals})
    except Exception:
        log.exception("could not roll back a failed turn")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    id: str
    started_at: str
    snapshot: Optional[str]  # base64 data URLs, dropped once sent
    crop: Optional[str]
    images_sent: bool = False
    turns: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def _prepare_image(png: bytes, max_side: int) -> str:
    """Resize a PNG to keep tokens reasonable and return it as a data URL."""
    if not png:
        raise ValueError("empty image")
    try:
        with Image.open(io.BytesIO(png)) as img:
            img = img.convert("RGB")
            img.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
    except Exception as exc:
        raise ValueError(f"could not read the image: {exc}") from exc
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _image_block(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url, "detail": IMAGE_DETAIL}}


def _first_message_content(session: _Session, question: str) -> list:
    return [
        {"type": "text", "text": "Snapshot with the Selection drawn in red:"},
        _image_block(session.snapshot),
        {"type": "text", "text": "Zoomed crop of the Selection:"},
        _image_block(session.crop),
        {"type": "text", "text": f"Question: {question}"},
    ]


def _get_session(session_id: str) -> _Session:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None:
        raise KeyError(f"unknown or finished session: {session_id!r}")
    return session


def _reply_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            return (msg.text or "").strip()
    return ""


def _usage(messages: list) -> dict:
    total = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0}
    for msg in messages:
        usage = getattr(msg, "usage_metadata", None)
        if not usage:
            continue
        total["input_tokens"] += usage.get("input_tokens", 0)
        total["output_tokens"] += usage.get("output_tokens", 0)
        total["cache_read"] += (usage.get("input_token_details") or {}).get(
            "cache_read", 0
        )
    return total


last_usage: dict[str, dict] = {}  # session id -> token use of its latest turn


def start_session(snapshot_png: bytes, crop_png: bytes) -> str:
    """Start a Session. snapshot_png already has the Selection drawn in red.
    Returns a session id."""
    snapshot = _prepare_image(snapshot_png, SNAPSHOT_MAX_SIDE)
    crop = _prepare_image(crop_png, CROP_MAX_SIDE)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    with _sessions_lock:
        session_id, n = stamp, 1
        while session_id in _sessions or _transcript_path(session_id).exists():
            n += 1
            session_id = f"{stamp}-{n}"
        _sessions[session_id] = _Session(
            id=session_id, started_at=_now(), snapshot=snapshot, crop=crop
        )
    return session_id


def ask(session_id: str, question: str) -> Explanation:
    """Answer the first question or a follow-up. Raises on API errors."""
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")
    session = _get_session(session_id)
    with session.lock:
        agent = _get_agent()
        config = {"configurable": {"thread_id": session.id}}
        keep = len(_messages(agent, config))
        with _explanations_lock:
            _explanations.pop(session.id, None)
        content = question if session.images_sent else _first_message_content(session, question)
        try:
            result = agent.invoke({"messages": [HumanMessage(content=content)]}, config)
        except Exception:
            _rollback(agent, config, keep)
            with _explanations_lock:
                _explanations.pop(session.id, None)
            raise

        messages = result["messages"]
        with _explanations_lock:
            shown = _explanations.pop(session.id, [])
        reply = _reply_text(messages)
        if shown:
            explanation = shown[-1]
            if not explanation.answer.strip():
                explanation = explanation.model_copy(update={"answer": reply or "…"})
        elif reply:
            explanation = Explanation(answer=reply)
        else:
            _rollback(agent, config, keep)
            raise RuntimeError("the model returned an empty reply")

        last_usage[session.id] = _usage(messages[keep:] if len(messages) > keep else messages)
        session.turns.append(
            # html is display-only; leaving it out keeps the consolidator's input small.
            {"question": question, "explanation": explanation.model_dump(exclude={"html"})}
        )
        if not session.images_sent:
            session.images_sent = True
            session.snapshot = session.crop = None  # keep memory small
        return explanation


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------

# One Consolidation at a time: end_session and the startup retry may overlap,
# and the Notes store is read-modify-write.
_consolidation_lock = threading.Lock()


def _consolidate_transcript(session_id: str) -> int:
    """Turn one saved Transcript into Notes. Returns how many Notes were saved."""
    with _consolidation_lock:
        transcript = _read_transcript(session_id)
        if transcript.get("consolidated"):
            return 0
        if not _has_concepts(transcript):
            transcript["consolidated"] = True
            _write_transcript(transcript)
            return 0

        before = {n["id"]: n for n in _load_notes()}
        _run["read"] = set()
        _run["upserts"] = 0
        agent = _get_agent()
        thread_id = f"consolidate-{session_id}"
        try:
            agent.invoke(
                {"messages": [HumanMessage(f"Consolidate Session {session_id}")]},
                {"configurable": {"thread_id": thread_id}},
            )
            if session_id not in _run["read"]:
                raise RuntimeError("the consolidator never read the Transcript")
            if _run["upserts"] == 0:
                raise RuntimeError("the consolidator saved no Notes")
        finally:
            _drop_thread(thread_id)
            _build_notebook_quietly()

        saved = sum(1 for n in _load_notes() if before.get(n["id"]) != n)
        transcript["consolidated"] = True
        _write_transcript(transcript)
        return saved


def end_session(session_id: str) -> int:
    """Save the Transcript, run Consolidation, rebuild the notebook.
    Returns how many Notes were saved. Raises if Consolidation fails; the
    Transcript is saved first, so it is retried on the next start."""
    _check_id(session_id)
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        # Already ended (or the app restarted): fall back to the saved file.
        if not _transcript_path(session_id).exists():
            raise KeyError(f"unknown session: {session_id!r}")
        return _consolidate_transcript(session_id)

    with session.lock:  # wait for an ask that is still running
        _write_transcript(
            {
                "id": session.id,
                "started_at": session.started_at,
                "ended_at": _now(),
                "consolidated": False,
                "turns": session.turns,
            }
        )
    _drop_thread(session.id)
    last_usage.pop(session.id, None)
    return _consolidate_transcript(session.id)


def consolidate_pending() -> int:
    """Consolidate any Transcripts not yet saved as Notes. Called once at startup.
    Returns Notes saved."""
    total, failed = 0, []
    if SESSIONS_DIR.exists():
        for path in sorted(SESSIONS_DIR.glob("*.json")):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("consolidated", True):
                    continue
                total += _consolidate_transcript(path.stem)
            except Exception:
                log.exception("could not consolidate %s", path.name)
                failed.append(path.stem)
    _build_notebook_quietly()
    if failed:
        raise RuntimeError(
            f"Consolidation failed for {len(failed)} Transcript(s): {', '.join(failed)}"
            f" ({total} Notes saved)"
        )
    return total


class LiveTranscriber:
    """Streams spoken questions to gpt-live-transcribe over one websocket.

    The app decides where a spoken turn ends (it detects the pause) and calls commit().
    on_partial(text) fires as words arrive, on_final(text) once per committed turn, and
    on_error(exc) if the connection fails. All three run on a background thread.
    """

    URL = "wss://api.openai.com/v1/realtime?intent=transcription"

    def __init__(self, on_partial, on_final, on_error):
        self._outbox: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._callbacks = (on_partial, on_final, on_error)
        self._closed = False
        threading.Thread(target=self._run, daemon=True).start()

    def append(self, pcm: bytes) -> None:
        """Queue 24 kHz mono 16-bit PCM audio. Safe to call before the connection is up."""
        self._outbox.put({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

    def commit(self) -> None:
        """End the current spoken turn; its final text arrives via on_final."""
        self._outbox.put({"type": "input_audio_buffer.commit"})

    def clear(self) -> None:
        """Drop audio sent since the last commit (a cough or a click, not a question)."""
        self._outbox.put({"type": "input_audio_buffer.clear"})

    def close(self) -> None:
        self._closed = True
        self._outbox.put(None)

    def _run(self) -> None:
        from websockets.sync.client import connect

        on_partial, on_final, on_error = self._callbacks
        session = {"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": LIVE_AUDIO_RATE},
            # No prompt: a maths-flavoured one made it hear "Why" as "Y".
            "transcription": {"model": LIVE_TRANSCRIBE_MODEL},
            "turn_detection": None,  # gpt-live-transcribe needs the client to find turn ends
        }}}}
        try:
            headers = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
            with connect(self.URL, additional_headers=headers) as ws:
                ws.send(json.dumps(session))
                threading.Thread(target=self._send_loop, args=(ws,), daemon=True).start()
                partial: dict[str, str] = {}
                for raw in ws:
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind == "conversation.item.input_audio_transcription.delta":
                        text = partial.get(event["item_id"], "") + event.get("delta", "")
                        partial[event["item_id"]] = text
                        on_partial(text)
                    elif kind == "conversation.item.input_audio_transcription.completed":
                        partial.pop(event["item_id"], None)
                        on_final((event.get("transcript") or "").strip())
                    elif kind == "error":
                        raise RuntimeError((event.get("error") or {}).get("message", "live transcription error"))
        except Exception as exc:  # includes the websocket closing under us
            if not self._closed:
                on_error(exc)

    def _send_loop(self, ws) -> None:
        try:
            while (message := self._outbox.get()) is not None:
                ws.send(json.dumps(message))
        except Exception:
            pass  # the receive loop reports connection errors
        finally:
            ws.close()


# ---------------------------------------------------------------------------
# Run the brain alone
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    end = "--end" in argv
    args = [a for a in argv if a != "--end"]
    if len(args) < 2:
        print('usage: python brain.py image.png "question" ["follow-up" ...] [--end]')
        return 2
    png = Path(args[0]).read_bytes()
    session_id = start_session(png, png)
    for question in args[1:]:
        explanation = ask(session_id, question)
        print(json.dumps(explanation.model_dump(), indent=2, ensure_ascii=False))
        print("usage:", last_usage.get(session_id), "\n")
    if end:
        print("Notes saved:", end_session(session_id))
        print("Notebook:", NOTEBOOK_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
