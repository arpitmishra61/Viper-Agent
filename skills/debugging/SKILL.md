---
name: debugging
description: Diagnose an error, failing test or unexpected behaviour methodically - reproduce, isolate, find the root cause, fix, verify. Use when something is broken and the cause is not obvious.
---

## Loop
1. **Reproduce.** Get the exact error text and the command that triggers it. Run it yourself if it is safe. If you cannot reproduce it, say so and ask for the missing detail.
2. **Read the error properly.** Start at the bottom of a traceback, find the first frame in the user's own code, and read that code.
3. **Form one hypothesis** and say what observation would confirm or kill it. Use `think` for this.
4. **Test it cheaply.** Add a print or assertion, run a smaller input, or check a version. Change one thing at a time.
5. **Find the root cause**, not the symptom. If a fix only silences the error, keep going.
6. **Fix minimally.** Do not rewrite surrounding code.
7. **Verify.** Re-run the original failing command and the nearby tests. Report the actual output.

## Common first checks
- Wrong working directory, wrong interpreter or virtualenv, missing dependency, version mismatch.
- A value that is None or empty where code assumed otherwise.
- Stale cache, stale build output, or an old process still running.
- Paths and line endings on Windows versus Unix.
- Environment variables not set in the shell that runs the code.

## Rules
- Do not install packages on the host to test an idea. Use the sandbox.
- Do not read `.env` to debug a configuration problem unless the task truly needs it; ask the user whether a variable is set instead.
- If you tried three fixes and none worked, stop, summarise what you learned, and ask the user.
- Tell the user what the root cause was, in one or two sentences, not only that it is fixed.
