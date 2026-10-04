---
name: morning-briefing
description: A short start-of-day briefing with weather, what is due in the user's notes, and a few headlines.
---

# Morning briefing

1. Find the user's city in your memories. If it is unknown and someone is there, use `ask_user`; otherwise skip the weather.
2. `get_weather` for that city: today's high and low, rain chance, and anything unusual.
3. `search_knowledge` for "due today", "deadline", "this week" and today's date. Mention at most three items, with the file they came from.
4. If the user has said which topics they follow, `web_search` for the latest on one or two of them and pick three headlines.
5. Keep the whole briefing under 120 words: weather, then what is due, then headlines. No preamble.
