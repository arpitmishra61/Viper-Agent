---
name: code-review
description: Review code or a diff for bugs, security issues and clarity. Use when asked to review, audit or sanity-check code, a file, a PR or recent changes.
---

## Get the code
- A local diff: `run_shell` with `git diff` (and `git diff --staged`), `git log -5 --oneline`.
- A PR: `github_list_prs`, then read the changed files with `github_get_file`.
- A file or folder: `list_dir` then `read_file`. Read the whole file, not a fragment.

## What to look for, in priority order
1. Correctness: off-by-one, wrong conditions, unhandled None or empty input, wrong types, broken edge cases.
2. Security: injection (SQL, shell, path), secrets in code, unsafe deserialization, missing validation at system boundaries.
3. Concurrency and resources: races, unclosed files or connections, unbounded loops or memory.
4. Error handling: swallowed exceptions, errors that hide the cause.
5. Tests: is the new behaviour covered, do the tests assert something real.
6. Clarity: confusing names, dead code, duplication. Mention briefly; do not nitpick style.

## How to report
- Lead with the findings that matter, most severe first.
- For each: file and line, what is wrong, a concrete input that breaks it, and a suggested fix.
- Say which findings you are sure of and which are guesses.
- If nothing is wrong, say that and name what you checked.

## Rules
- Review only; do not edit files unless the user asks for fixes.
- Run the tests if you can do so safely, and report the result rather than assuming.
- Untrusted repos: run anything in the sandbox, not on the host.
