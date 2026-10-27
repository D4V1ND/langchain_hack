# Brain: agent and memory (Person B)

You own `brain.py` and everything in `data/`: what goes into each prompt, how a Session is remembered, caching, Transcripts and Notes. The app calls you only through the 5 functions in the [contract](SPEC.md#4-the-contract) and never touches your files.

**First job (first 15 minutes):** push `brain.py` with the exact contract signatures returning fake data, so Person A isn't blocked.

---

## Tasks, in order

| # | Task | Done when |
|---|---|---|
| 1 | **Fake brain.** Contract signatures, with `ask()` returning a fixed "Area of a circle" Explanation | Person A can import it and render the result |
| 2 | **Tutor.** `create_deep_agent` with an OpenAI image-reading model (kept in one constant), the tutor system prompt and the `show_explanation` tool. Give each Session its own conversation thread (checkpointer) | `python brain.py test.png "Why is r squared?"` prints an Explanation |
| 3 | **Images.** The first message carries the Snapshot, the crop and question 1. Follow-ups send text only. Resize the images to keep tokens reasonable, and use the same `detail` setting every time | A follow-up answer still knows what's in the image without it being re-sent |
| 4 | **Prompt caching.** Follow the rules below | `cache_read` is above 0 on the second question |
| 5 | **Transcript.** `end_session()` writes `data/sessions/<time>.json` before anything else | The file exists even when the network is off |
| 6 | **Notes store.** Read and write `notes.json` with `list_notes`, `get_note` and `upsert_note`. Enforce "lists only grow" in code, not in the prompt | One `assert`-based check proves `upsert_note` can't remove an existing mistake |
| 7 | **Consolidator.** A sub-agent with `read_transcript`, which the tutor hands work to through Deep Agents' `task` tool. Then mark the Transcript `consolidated: true` | One Session about A = πr² plus the diameter question gives 2 Notes |
| 8 | **`consolidate_pending()`.** Consolidate every Transcript that is still `consolidated: false` | Kill the app mid-save, restart it, and the Notes appear |
| 9 | **`notebook.html`.** Rebuild it from `notes.json` after every save. Newest first, each Note collapsible, a search box, KaTeX for statements | It opens in a browser, and the search filters Notes |
| 10 | **`transcribe()`.** Send the WAV to OpenAI's `gpt-transcribe` (constant `TRANSCRIBE_MODEL`, overridable with `STUDYLENS_TRANSCRIBE_MODEL`), with a short maths-context prompt | A spoken "Why is r squared?" comes back as that text |

---

## Agent

**Tutor (main Deep Agent).** It runs on every question and remembers the Session.

| Tool | Does |
|---|---|
| `show_explanation(explanation)` | Records the Explanation for this turn |

`ask()` returns the Explanation from the last `show_explanation` call in that turn. If the agent didn't call it, return `Explanation(answer=<its text reply>)`.

**Consolidator (sub-agent).** The tutor hands work to it from `end_session()` and `consolidate_pending()`.

| Tool | Does |
|---|---|
| `read_transcript(id)` | Returns the Session's questions and Explanations |
| `list_notes()` | Returns every Note's id, name and statement |
| `get_note(id)` | Returns one full Note |
| `upsert_note(id or none, fields)` | Creates a Note, or adds to an existing one (lists only grow) |

Consolidation works from the Transcript file, not from memory, so a normal close and a retry on startup go through the same code.

---

## Data

### Note (one per Concept, in `notes.json`)

| Field | Rule |
|---|---|
| `id` | Set when the Note is created, never changes |
| `name`, `statement`, `variables`, `when_to_use` | Change only with a stated reason |
| `mistakes` | List; only grows |
| `examples` | List; only grows. Each `why_here` from a Session becomes one example |
| `insights` | List; only grows. Key points from follow-up questions |
| `updated_at` | Used to sort the Notebook |

### Files

```
data/
├── notes.json          all Notes (a JSON list)
├── notebook.html       generated from notes.json
└── sessions/
    └── 2026-10-27T14-03-11.json   one Transcript: questions, Explanations, consolidated: true/false
```

The Snapshot image is never saved to disk.

---

## Prompt caching

OpenAI reuses the start of a prompt (the "prefix") when it matches a recent call exactly. Deep Agents uses this automatically. You only have to avoid breaking it.

Every follow-up question re-sends the whole Session, so everything above the newest question should come from the cache:

```
[Deep Agents instructions + tool definitions]   same every call     → cached
[tutor system prompt]                           same every call     → cached
[Snapshot + crop + question 1]                  same all Session    → cached
[Explanation 1 … question N-1]                  already sent        → cached
[question N]                                    new                 → not cached
```

**Rules**

1. Keep the system prompt fixed: no time, Session id or notes list in it.
2. Send the Snapshot and crop once, in the first message, with the same image `detail` setting every time.
3. The consolidator gets Notes through `list_notes`, never through its system prompt.
4. Never edit or trim earlier messages during a Session.

**Check:** on the second question of a Session, `usage_metadata["input_token_details"]["cache_read"]` should be above 0 (also visible in LangSmith). If it's 0, something near the top of the prompt is changing.

**Limits:** prompts need about 1,024+ tokens to cache, the cache clears after a few minutes idle, and on GPT-5.6+ writing to the cache costs 1.25× normal input, so single-question Sessions gain nothing.

---

## Errors

| Problem | What you do |
|---|---|
| The Selection is unreadable or ambiguous | Return an Explanation with only `answer` filled in, holding a clarifying question |
| API error during `ask()` | Raise. The app shows "try again" |
| Consolidation fails | Raise after the Transcript is saved. Leave it as `consolidated: false` so it's retried |

---

## Testing alone

Take a screenshot of a worked solution, draw a red shape on it in Paint, and run:

```bash
python brain.py test.png "Why is r squared?"
```

Then add a follow-up and an `end_session()` call to the same script to test Consolidation without the app.
