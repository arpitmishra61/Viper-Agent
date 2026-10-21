---
name: project-scaffold
description: Create a new local project or feature - folder layout, starter files, config, README. Use when asked to start, scaffold, set up or bootstrap a project on the user's machine.
---

## Before writing anything
1. Ask or infer: language, framework, project name, target folder. If one answer would change the layout a lot, ask once; otherwise pick a sensible default and say what you chose.
2. `list_dir` the target so you do not overwrite existing work.

## Build it on the host
The user wants these files on their machine, so use the host:
- `write_file` for every file. It is safer and clearer than shell redirection.
- `run_shell` with `sandbox=false` only for `mkdir` and read-only checks such as `git status`.

## Typical contents
- Source folder with a minimal runnable entry point.
- A tests folder with one passing test.
- Dependency file (`requirements.txt`, `package.json`, `pyproject.toml`).
- `.gitignore` covering virtualenvs, build output and `.env`.
- A short README: what it is, how to install, how to run, how to test.
- A `.env.example` listing needed variables with empty values. Never write real secrets.

## Dependencies
Installing packages (`pip install`, `npm install`, `uv add`) happens in the sandbox, never on the host. To check that the scaffold works, copy it into the sandbox, install and run the tests there. Give the user the exact install command for their own machine.

## Finish
- List what was created as a short tree.
- Give the three commands to install, run and test.
- Do not run `git init` or commit unless asked.
