# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

StudyLens is a Windows desktop study assistant built in 3 hours for a hackathon by a team of two. The student presses a hotkey, draws any shape around a confusing formula on screen and types a question. A LangChain Deep Agent explains it in a floating panel, and when the panel closes it saves what was learned as Notes.

**Status:** spec only, no code yet. The spec is the source of truth:

- [SPEC.md](SPEC.md): product, decisions, the app↔brain contract (§4) and the 3-hour plan
- [SPEC-APP.md](SPEC-APP.md): desktop app tasks (Person A)
- [SPEC-AGENT.md](SPEC-AGENT.md): agent, context, caching and Notes tasks (Person B)
- [CONTEXT.md](CONTEXT.md): glossary. Use these exact terms (Snapshot, Selection, Concept, Explanation, Session, Transcript, Note, Notebook, Consolidation) in code, prompts and commits, and avoid the words it lists under _Avoid_

## Commands

Windows PowerShell, pip with a `.venv`. Don't use uv, poetry or conda, even where Deep Agents docs say `uv add`.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install PySide6 pynput deepagents langchain-openai python-dotenv
python app.py                                   # run the desktop app
python brain.py test.png "Why is r squared?"    # run the brain alone on a screenshot
```

`OPENAI_API_KEY` lives in `.env`, which is git-ignored. `brain.py` loads it with `load_dotenv()`. `.env.example` shows the required keys.

## Architecture

Two halves that meet only through 4 functions in `brain.py` (SPEC.md §4):

| Side | Files | Owns |
|---|---|---|
| App (PySide6) | `app.py`, `panel.html` | Hotkey, Snapshot, drawing, Explanation Panel, tray |
| Brain (Deep Agents) | `brain.py`, `data/` | Tutor agent, consolidator sub-agent, Transcripts, `notes.json`, `notebook.html` |

- The app never reads or writes `data/`, and the brain never imports Qt. Changing a contract signature means updating SPEC.md §4 too.
- Every brain function blocks for seconds. The app calls them from worker threads and sends results back to the UI thread with Qt signals. `pynput` hotkey callbacks also run off the UI thread, so they must emit a signal, never touch widgets.
- The tutor runs one checkpointer thread per Session (`config={"configurable": {"thread_id": session_id}}`). It delegates Consolidation to the consolidator sub-agent through Deep Agents' built-in `task` tool.
- Consolidation always reads the Transcript file (`data/sessions/*.json`), never in-memory state. That way a normal close and the startup retry (`consolidate_pending`) share one path. Write the Transcript before consolidating.
- "Note lists only grow" is enforced in `upsert_note` code, not in the prompt. Don't move it into the prompt.
- `notebook.html` is generated from `notes.json`. Never edit it by hand or have the agent edit it.

## Deep Agents

- The API moves fast. Check https://docs.langchain.com/oss/python/deepagents before relying on a signature from memory.
- `create_deep_agent(model="openai:<model>", tools=[...], system_prompt=..., subagents=[...], checkpointer=...)`. The model id is one constant in `brain.py`.
- Sub-agents are dicts with `name`, `description`, `system_prompt` and `tools`. The main agent picks one by its `description`, so make that specific.
- The default middleware includes filesystem tools, a general-purpose sub-agent, summarization and prompt caching. Tools that aren't needed add tokens to every call.

## Prompt caching rules (SPEC-AGENT.md)

OpenAI caches the prompt prefix automatically, and these rules keep the cache working:

- Keep system prompts static: no time, Session id or notes list.
- Send the Snapshot and crop once, in the first message of a Session, with a fixed image `detail`.
- The consolidator gets Notes only through the `list_notes` tool.
- Never edit or trim earlier messages. Deep Agents' summarization middleware rewrites history on long conversations, so keep Sessions short or check that it doesn't trigger.
- To check it's working, `usage_metadata["input_token_details"]["cache_read"]` should be above 0 on a Session's second question.
