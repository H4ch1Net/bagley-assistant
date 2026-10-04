---
name: research-a-question
description: Answer a factual or "what's the latest on" question from several web sources, cross-checked and cited.
---

# Research a question

1. Rewrite the question as one or two focused search queries. Use `web_search` for each.
2. Pick the two or three most reliable results (official sites, documentation, established outlets). Skip forums and SEO pages unless nothing else covers it.
3. Read each with `fetch_webpage`. If the context window is small or there are several sources, use `delegate_task` with one subtask per source, asking each for a short factual summary with the URL.
4. Compare the sources. Where they disagree, say so and prefer the more recent or more authoritative one.
5. Answer in a few sentences or a short list. Put the source URLs at the end. Say clearly what you could not confirm.
