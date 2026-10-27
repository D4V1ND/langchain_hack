# StudyLens: Spec

**Mark it. Understand it. Remember it.**

Press a hotkey, draw any shape around a confusing formula or step on your screen, type a question, and get an explanation in a small floating panel. When you close the panel, what you learned is saved as notes in your personal Notebook.

Words in **bold** are defined in [CONTEXT.md](CONTEXT.md).

## Who builds what

| Part | Owner | Spec |
|---|---|---|
| **Desktop app**: everything on screen (hotkey, drawing, panel, tray) | Person A | [SPEC-APP.md](SPEC-APP.md) |
| **Brain**: agent, prompts, context, caching, Notes on disk | Person B (context and caching) | [SPEC-AGENT.md](SPEC-AGENT.md) |

The two parts meet only through the 4 functions in [section 4](#4-the-contract). Agree on those first, then work apart.

---

## 1. What the demo does (3-hour MVP)

1. Press the hotkey. The screen under your mouse freezes (a **Snapshot**).
2. Draw any shape around what confuses you (a **Selection**).
3. The **Explanation Panel** opens next to it with a text box. Type your question.
4. The agent reads the Snapshot and your question, then shows an **Explanation**.
5. Ask follow-ups in the same box. The panel updates.
6. Close the panel. The conversation is saved as a **Transcript**, then turned into **Notes** (**Consolidation**).
7. Open the **Notebook** from the tray icon to revise.

**Not in the demo:** voice input and spoken answers (add later if time allows), checking maths with a tool, diagrams in explanations, and several panels at once.

---

## 2. Example

The student is looking at a worked solution in a PDF and doesn't get one line.

| Student | StudyLens |
|---|---|
| Presses the hotkey and scribbles around `A = πr²` | Freezes the screen, then opens the panel beside the scribble |
| Types: "Why is r squared?" | Shows: **Area of a circle**, `A = πr²`, r = radius. Area is two-dimensional, so the length gets multiplied by itself… |
| Types: "What if I'm given the diameter?" | Switches the panel to **Radius from diameter**, `r = d/2`, and shows how to plug it back in |
| Closes the panel | Saves the Transcript, then the background job writes 2 Notes: "Area of a circle" (new or enriched) and "Radius from diameter" (new) |
| Opens the Notebook from the tray | Both Notes appear at the top |

---

## 3. Decisions

| Topic | Decision | Why |
|---|---|---|
| Where it runs | Desktop overlay on the real screen | That's the product. A web page with an uploaded image would be a fake demo |
| Tech | Python + PySide6 (Qt for Python) | One language and one process. Qt has the see-through window, screenshots and an HTML view built in |
| Trigger | Global hotkey (`pynput`), which freezes the screen | Works from any app, and the drawing always lines up with the image |
| Monitors | Freeze the monitor the mouse is on | The student is looking where they're pointing |
| What the AI sees | Full Snapshot with the Selection drawn on it in red, plus a zoomed crop | It sees both the surrounding problem and exactly what you mean. No text-recognition (OCR) step |
| Model | OpenAI GPT (a model that reads images), set in one constant | The team has an OpenAI key |
| First question | Typed in the panel's text box, the same box used for follow-ups | One input box to build |
| Panel content | The agent fills fixed fields, shown with one HTML template and KaTeX for the maths | Same look every time, and the same fields feed the Notes |
| Several Concepts | The panel shows one Concept at a time (whatever the last question was about). Consolidation saves every Concept discussed | Follow-up insights aren't lost |
| What a Note is about | A **Concept**: a formula, rule, theorem or method | Steps like "odd terms cancel by symmetry" also get Notes |
| New Selection while a panel is open | Close the old panel (which consolidates it), then start the new one | One panel and one Session at a time |
| Duplicates | The agent sees every Note's id, name and statement, then decides whether to create a new Note or update an existing one | Treats "πr^2" and "\pi r^{2}" as the same thing; no vector database needed |
| Updating a Note | Lists (mistakes, examples, insights) only grow. Name, statement and "when to use" change only if the agent gives a reason | A bad AI run can't wipe earlier learning |
| Storage | `notes.json` holds the data. `notebook.html` is rebuilt from it after every save | JSON is easy to update safely; the HTML file doubles as the export |
| Raw conversation | Saved to `sessions/` before Consolidation and kept | Nothing is lost if saving fails |
| Failed save | Each Transcript has a `consolidated` flag, and any not yet consolidated are retried on the next start. A tray notification shows the result | Recovers automatically |
| Opening the Notebook | Tray icon menu: **Open Notebook**, **Quit** | Always reachable |
| Notebook page | Most recently updated first, each Note collapsible, search box at the top | Shows what you just learned and finds old Notes fast |
| Agent | One Deep Agent tutors; a "consolidator" sub-agent saves Notes | Uses Deep Agents in both halves |
| Prompt caching | Use OpenAI's automatic caching (Deep Agents turns it on) and keep the start of every prompt identical | Faster follow-up answers, cheaper image re-sends, no extra code |

---

## 4. The contract

The app calls these functions in `brain.py`. Nothing else crosses the line: the app never reads or writes `data/`, and the brain never touches the screen.

```python
# brain.py (owned by Person B)
from pathlib import Path
from pydantic import BaseModel

class Variable(BaseModel):
    symbol: str
    meaning: str

class Explanation(BaseModel):
    answer: str                      # always filled; a clarifying question if the Selection is unclear
    name: str = ""
    statement: str = ""              # LaTeX or a plain sentence
    variables: list[Variable] = []
    when_to_use: str = ""
    why_here: str = ""
    mistakes: list[str] = []
    example: str = ""

NOTEBOOK_PATH: Path                  # data/notebook.html; the tray menu opens it

def start_session(snapshot_png: bytes, crop_png: bytes) -> str:
    """Start a Session. snapshot_png already has the Selection drawn in red. Returns a session id."""

def ask(session_id: str, question: str) -> Explanation:
    """Answer the first question or a follow-up. Raises on API errors."""

def end_session(session_id: str) -> int:
    """Save the Transcript, run Consolidation, rebuild the notebook. Returns how many Notes were saved.
    Raises if Consolidation fails; the Transcript is saved first, so it is retried on the next start."""

def consolidate_pending() -> int:
    """Consolidate any Transcripts not yet saved as Notes. Called once at startup. Returns Notes saved."""
```

**Rules for both sides**

- Every function blocks and can take several seconds. The app always calls them from a worker thread, never the UI thread.
- The app sends full-size PNGs. The brain decides how to resize them.
- Changing a signature means telling the other person first.

---

## 5. 3-hour plan

- **0:00–0:15, together:** agree on the contract, set up the `.venv`, and Person B pushes a **fake `brain.py`** with the exact signatures that returns a fixed "Area of a circle" Explanation. Person A is never blocked after this.
- **0:15–1:45, apart:** each works down their own task list.
- **1:45–2:15, together:** swap the fake brain for the real one and run the full flow end to end.
- **2:15–2:45:** fix what broke.
- **2:45–3:00:** rehearse the demo.

---

## 6. Setup

- Install into the `.venv`: `PySide6`, `pynput`, `deepagents`, `langchain-openai`, `python-dotenv`
- Put `OPENAI_API_KEY` in `.env` (git-ignored; copy `.env.example`). `brain.py` loads it with `load_dotenv()`
- `.gitignore` already covers `.env`, `.venv/` and `data/`
- Each person edits only their own files (`app.py` and `panel.html` for A, `brain.py` for B), so both can push to `main` with few conflicts

---

## 7. Known risk

**Privacy.** The whole frozen screen goes to OpenAI. The student chooses when, but anything else on that screen goes along with it.

---

## Later

- Voice questions and spoken answers
- A maths-checking tool (SymPy)
- Diagrams in Explanations
- Linking a new Selection to the current Session ("how does this step connect to the last one?")
- Grouping Notes by subject
