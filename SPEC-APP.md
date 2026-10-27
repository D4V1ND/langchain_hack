# Desktop app (Person A)

You own everything the student sees and touches. You call the brain only through the 4 functions in the [contract](SPEC.md#4-the-contract), and you never read or write `data/`.

**Your files:** `app.py` (windows, hotkey, tray) and `panel.html` (the Explanation template).

---

## Tasks, in order

| # | Task | Done when |
|---|---|---|
| 1 | **Display-scaling check.** Grab the screen under the mouse with `QScreen.grabWindow(0)`, show it full-screen, and draw a dot where you click | The dot lands exactly under the cursor at 100%, 125% and 150% Windows scaling |
| 2 | **Hotkey.** Use a `pynput` global hotkey, `Ctrl+Alt+L`, kept in one constant. Pass the key press to the Qt thread with a signal | Pressing it from any app opens the frozen screen |
| 3 | **Draw.** Freehand drawing with the mouse. Releasing the button ends the Selection, and `Esc` cancels | Any shape works, and tiny accidental clicks (under 10 px) are ignored |
| 4 | **Images.** Make a copy of the Snapshot with the shape drawn in red (3–4 px), plus a crop of the shape's bounding box with some padding | Both are PNG bytes. Write them to disk once to check them by eye |
| 5 | **Panel window.** A frameless, always-on-top window with a `QWebEngineView` (showing `panel.html`) and a `QLineEdit` below it. Place it beside the Selection, and flip it to the other side if it would go off screen | The panel opens next to the Selection with an empty, focused text box |
| 6 | **Ask.** On Enter, disable the box, show "Thinking…", call `start_session()` (first question only) and then `ask()` in a worker thread, then show the Explanation | The panel updates, the box is enabled again, and the UI never freezes |
| 7 | **`panel.html`.** A shell page that blocks outside loads. Show the Explanation's `html` inside it (or `answer` when `html` is empty), with JavaScript turned off and link clicks blocked | The AI's page shows with its MathML maths and SVG diagrams, and a `<script>` in it never runs |
| 8 | **Close.** Closing the panel, or pressing the hotkey while a panel is open, calls `end_session()` in a worker thread | A new Selection works straight away while the old one saves |
| 9 | **Tray.** An icon with **Open Notebook** (opens `NOTEBOOK_PATH` in the default browser) and **Quit**. At startup, call `consolidate_pending()` in a worker thread | Notifications show "Saved N notes" or "Couldn't save notes, will retry next start" |

---

## Errors you handle

| Problem | What the student sees |
|---|---|
| `ask()` raises | The panel says "Couldn't reach the AI, try again", and the question stays in the box |
| `end_session()` or `consolidate_pending()` raises | A tray notification: "Couldn't save notes, will retry next start" |
| The Explanation has only `answer` filled in | The panel shows just the answer (usually a clarifying question) |

---

## Working without the real brain

Use the fake `brain.py` that Person B pushes in the first 15 minutes. It returns a fixed Explanation instantly. Add `time.sleep(3)` inside it to check that "Thinking…" shows and that the UI doesn't freeze.

---

## Watch out for

- **Display scaling.** Do task 1 first. If the coordinates are off, everything after it is off too.
- **Qt threads.** Only touch Qt widgets from the UI thread. Send results back from worker threads with a signal.
- **Hotkey thread.** `pynput` runs on its own thread. Never open windows from it directly; emit a signal instead.
