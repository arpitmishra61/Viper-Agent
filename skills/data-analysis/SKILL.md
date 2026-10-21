---
name: data-analysis
description: Analyse data or do exact computation with code - counting, sorting, CSV/JSON/Excel, statistics, charts, image processing. Use whenever being off by one is not acceptable.
---

## Principle
Never guess at counts or exact values. Write code and run it.

## Sandbox rule
`run_python` runs in the sandbox by default. Keep it that way for third-party packages, file work, network calls, image processing and data analysis. Use `sandbox=false` only for pure stdlib math or in-memory string work with no files, subprocesses or network.

## Getting a local file into the sandbox
Read it locally with `read_file` (or `run_shell` with `sandbox=false`), then write it into the sandbox and work there. Needing a local file is never a reason to install packages on the host.

## Method
1. Inspect first: shape, columns, dtypes, missing values, a few sample rows. Print them.
2. State the question as a computation before writing it.
3. Do the computation, then sanity-check it (row counts add up, totals match, ranges plausible).
4. Report the number, the method in one line, and any caveat such as dropped rows or assumptions.

## Output
- Show a small table for results, not a data dump.
- For charts, save an image file and tell the user where it is.
- If the result needs to exist on the user's machine, save it with `write_file`; sandbox files do not appear locally.

## Pitfalls
- Dates and timezones, string-typed numbers, duplicate rows, and empty strings that should be nulls.
- Excel files: cell values only; formulas are not evaluated.
