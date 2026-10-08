# Study mode, flashcards and quizzes

Study mode turns Bagley into a patient tutor. It explains with examples from real IT jobs, quizzes you one question at a time, builds questions from your own course notes, keeps flashcards with spaced repetition and times your study sessions.

## Study mode

Pick **Study** (`STDY`) as a chat's mode, or make it the default for new chats (preference `default_mode: "study"`). In study mode the flashcard, quiz and session tools are loaded from the first message, together with the knowledge base. In other modes they load when a message mentions flashcards, quizzes, revision, exams or studying.

Try:

- "Quiz me on subnetting, three questions, one at a time."
- "Make flashcards from my notes on Active Directory."
- "Review my flashcards that are due."
- "I want to study OSPF for 45 minutes."

## Flashcards

Cards live in Bagley's database, grouped in decks. Each card has a front (a question or a term) and a back (the answer). Adding a card whose front is already in the deck does nothing, so asking twice doesn't create duplicates; deck names and fronts are compared without regard to case.

Reviews use the SM-2 algorithm. After seeing the answer you grade how well you knew it:

| Grade | Meaning |
|---|---|
| 0 | Blank |
| 1 | Wrong |
| 2 | Wrong, but it felt familiar |
| 3 | Right, with effort |
| 4 | Right after a pause |
| 5 | Instant |

A grade of 3 or more moves the card further out: one day, then six, then the previous interval times the card's ease (2.5 to start). A grade below 3 counts a lapse and brings the card back tomorrow. Each grade nudges the ease up or down, never below 1.3, so hard cards come back more often. Cards fall due at the start of their day, so one session a day catches everything that is due.

### In a chat

The model uses these tools:

| Tool | What it does |
|---|---|
| `add_flashcards` | Save cards (`[{front, back}]`) to a deck |
| `due_flashcards` | Cards due for review, oldest first |
| `grade_flashcard` | Record a grade (0-5) and schedule the next review |
| `list_decks` | Decks with their total, due and new cards |
| `study_material` | Passages about a topic from your knowledge base, to build quiz questions from |
| `start_study_session` | A focus block with a reminder when it ends and another when the break is over |

None of them ask for approval. Scheduled runs can read cards but can't add, grade or schedule anything.

### In the terminal

```sh
bagley cards                          # review everything that is due
bagley cards subnetting               # review one deck
bagley cards -n 50                    # up to 50 cards
bagley cards list                     # decks: total, due, new
bagley cards list --json
bagley cards add Ports "Port of SSH?" "22"
```

A review shows the front, waits for <kbd>Enter</kbd>, shows the back and asks for a grade: `0` to `5`, or `a` (again, 1), `h` (hard, 3), `g` (good, 4), `e` (easy, 5). A card you get wrong comes back once more at the end of the session. `q` stops early; the cards you already graded keep their grades.

```
» REVIEW // PORTS  002 DUE

  [001/002] Ports
  Q  Port of SSH?
  ENTER REVEALS ›
  A  22
  GRADE 0-5 // A H G E // Q QUITS › g
  [OK] NEXT 091026 // 001 DAYS
```

`bagley cards` uses the running server when there is one and the database directly otherwise.

### Import and export

Cards export to CSV with a `front,back,deck` header:

```sh
curl -o ports.csv 'http://127.0.0.1:8765/api/flashcards/export?deck=Ports'
curl -X POST --data-binary @ports.csv 'http://127.0.0.1:8765/api/flashcards/import?deck=Networking'
```

Import accepts rows of `front,back` or `front,back,deck`, with or without the header, separated by commas, tabs (Anki and Quizlet exports) or semicolons. The `deck` parameter puts every card in that deck; without it the third column is used, then "General". Files up to 2 MB and 5,000 cards. Cells that start with `=`, `+`, `-` or `@` are written with a leading `'` so spreadsheets don't run them as formulas, and read back without it.

### API

| Endpoint | Body | Returns |
|---|---|---|
| `GET /api/flashcards/decks` | | `[{deck, total, due, new}]` |
| `GET /api/flashcards?deck=&due=1&limit=500` | | Cards, oldest due first with `due=1` |
| `POST /api/flashcards` | `{deck, cards: [{front, back}], source?}` | `{deck, added, ids, duplicates}` (201) |
| `POST /api/flashcards/{id}/grade` | `{quality: 0-5}` | The updated card |
| `DELETE /api/flashcards/{id}` | | `{ok: true}` |
| `GET /api/flashcards/export?deck=` | | CSV |
| `POST /api/flashcards/import?deck=` | CSV | `{added, duplicates, skipped, decks}` |

A card is `{id, deck, front, back, ease, interval, reps, lapses, due, created_at, updated_at, source, new, is_due}`; `interval` is in days and `due` is a Unix timestamp. Changes are broadcast to open windows as `flashcards.changed`.

## Quizzes from your notes

Add your course folders to the knowledge base (**Settings → Knowledge**), or your Obsidian vault (see [Your own activity](life.md#setup)). When you ask for a quiz on a topic, Bagley calls `study_material`, which searches your notes and hands the model the matching passages. Questions are built from those passages and say which note they come from; if nothing matches, Bagley says so and offers a quiz from general knowledge instead. Ask it to turn a quiz into flashcards afterwards.

## Study sessions

"Study OSPF for 45 minutes" starts a session: a reminder when the focus block ends and another when the break is over, both in the chat you started from (and on your desktop and phone if notifications are set up). The break is 5 minutes for a 25-minute block and scales with longer ones (a fifth of the block). Blocks last 5 to 180 minutes. The reminders are ordinary automations, so you can cancel them in **Settings → Automations** or by asking.
