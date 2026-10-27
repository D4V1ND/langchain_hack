# StudyLens

A desktop study assistant: the student draws around a confusing formula or step on their screen, asks about it, and the explanation is later kept as revision notes.

## Language

**Snapshot**:
The frozen image of the whole screen, taken the moment the student presses the hotkey. The student draws on it.
_Avoid_: Capture, screen grab

**Selection**:
The freehand shape the student draws on a Snapshot to point at what confuses them. Any shape counts.
_Avoid_: Circle, lasso, highlight, mark

**Concept**:
One formula, rule, theorem or method a student can learn, such as "area of a circle" or "odd terms cancel by symmetry". Its statement can be maths or a sentence.
_Avoid_: Formula (when it might not be one), topic, idea

**Explanation**:
The structured account of one Concept: a short answer to the latest question, the statement, its name, its variables, when to use it, why it fits this problem, common mistakes and an example.
_Avoid_: Answer, response, card

**Explanation Panel**:
The floating window next to the Selection that shows the current Explanation and takes follow-up questions.
_Avoid_: Popup, mini-browser, chat window

**Session**:
One Selection's conversation, from the first question until the Explanation Panel closes. It can cover several Concepts.
_Avoid_: Chat, conversation, tutoring session

**Transcript**:
The saved record of everything asked and answered in one Session, kept even after Consolidation.
_Avoid_: Log, history, chat log

## Notebook

**Note**:
The saved revision material for exactly one Concept. A Concept has at most one Note.
_Avoid_: Card, entry, page

**Notebook**:
The student's collection of Notes, kept across Sessions.
_Avoid_: Study notebook, study guide, memory

**Consolidation**:
Turning a finished Session into Notes: creating a Note for each new Concept discussed and enriching the existing Note for each known one.
_Avoid_: Summarising, saving, memory update
