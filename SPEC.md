# StudyLens: Spec

**Mark it. Understand it. Remember it.**

Press a hotkey, draw any shape around a confusing formula or step on your screen, type a question, and get an explanation in a small floating panel. When you close the panel, what you learned is saved as notes in your personal Notebook.

Words in **bold** are defined in [CONTEXT.md](CONTEXT.md).

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

---

## 4. Data

### Explanation (what the panel shows, one Concept)

| Field | Example |
|---|---|
| `answer` | "Area is 2-D, so the radius is multiplied by itself." |
| `name` | Area of a circle |
| `statement` | `A = \pi r^2` (LaTeX or a plain sentence) |
| `variables` | `[{symbol: "A", meaning: "area"}, {symbol: "r", meaning: "radius"}]` |
| `when_to_use` | You need the space inside a circle |
| `why_here` | The problem asks for the area of the circular base |
| `mistakes` | `["Using the diameter instead of the radius"]` |
| `example` | r = 3 → A = 9π ≈ 28.3 |

### Note (one per Concept, in `notes.json`)

| Field | Rule |
|---|---|
| `id` | Set when the Note is created, never changes |
| `name`, `statement`, `variables`, `when_to_use` | Change only with a stated reason |
| `mistakes` | List; only grows |
| `examples` | List; only grows. Each `why_here` from a Session becomes one example |
| `insights` | List; only grows. Key points from follow-up questions |
| `updated_at` | Used to sort the Notebook |

### Files on disk (`data/` folder)

```
data/
├── notes.json          all Notes
├── notebook.html       generated from notes.json; open it to revise
└── sessions/
    └── 2026-10-27T14-03-11.json   one Transcript: questions, Explanations, consolidated: true/false
```

The Snapshot image itself is not saved to disk.

---

## 5. Agent

**Tutor (main Deep Agent).** It runs on every question and remembers the Session.

| Tool | Does |
|---|---|
| `show_explanation(explanation)` | Sends the Explanation fields to the panel |

The Snapshot and Selection go straight into the message as images, so no tool is needed to "read the screen".

**Consolidator (sub-agent).** The tutor hands work to it when a panel closes or when the app starts with unsaved Transcripts.

| Tool | Does |
|---|---|
| `read_transcript(id)` | Returns the Session's questions and Explanations |
| `list_notes()` | Returns every Note's id, name and statement |
| `get_note(id)` | Returns one full Note |
| `upsert_note(id or none, fields)` | Creates a Note, or adds to an existing one (lists only grow) |

After the consolidator finishes, plain code (not an agent tool) marks the Transcript `consolidated: true` and rebuilds `notebook.html`.

Consolidation works from the Transcript file, not from memory, so the same code handles a normal close and a retry on startup.

---

## 6. Errors

| Problem | What happens |
|---|---|
| The Selection is unreadable or ambiguous | The agent asks a clarifying question in `answer` and leaves the other fields empty |
| API error while tutoring | The panel shows "Couldn't reach the AI, try again" and the question stays in the box |
| Consolidation fails, or the app quits mid-save | The Transcript stays `consolidated: false` and is retried on the next start |
| The agent is still thinking | The text box is disabled and the panel shows "Thinking…". Agent calls run off the UI thread, so the app never freezes |

---

## 7. Setup

- Install: `PySide6`, `pynput`, `deepagents`, `langchain-openai` into the `.venv`
- Set `OPENAI_API_KEY` as an environment variable
- Hotkey default: `Ctrl+Alt+L`, set in one constant
- KaTeX loads from a CDN (an internet connection is needed for OpenAI anyway)

---

## 8. Risks to check early

1. **Windows display scaling.** At 125% or 150% scaling, Qt's coordinates and the screenshot's pixels differ, so the red shape can land in the wrong place. Test this first.
2. **Privacy.** The whole frozen screen goes to OpenAI. The student chooses when, but anything else on that screen goes along with it.
3. **Speed.** A Deep Agent turn takes a few seconds. Make sure "Thinking…" shows straight away.

---

## Later

- Voice questions and spoken answers
- A maths-checking tool (SymPy)
- Diagrams in Explanations
- Linking a new Selection to the current Session ("how does this step connect to the last one?")
- Grouping Notes by subject
