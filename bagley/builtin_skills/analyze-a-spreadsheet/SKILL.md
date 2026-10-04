---
name: analyze-a-spreadsheet
description: Summarise a CSV or Excel file in the workspace and chart what matters.
---

# Analyze a spreadsheet

1. Locate the file with `list_files` or `search_files`. Read the first lines with `read_file` to see the columns.
2. If `run_python` is available, write one script that loads the file (pandas if installed, otherwise the csv module), prints the row count, column types, totals and averages for numeric columns, and the top rows by the most meaningful column.
3. Leave one clear matplotlib chart open (a bar or line chart of the main measure over time or by category). It is shown to the user automatically.
4. If `run_python` is not available, work from `read_file` and `calculate` on a sample, and say that the full analysis needs the shell enabled.
5. Report three to five findings in plain language, with the numbers.
