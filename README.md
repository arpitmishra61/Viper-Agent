# Viper

A minimal terminal AI agent built in Python. Multi-provider, fully agentic, runs locally.

```
 ██╗   ██╗██╗██████╗ ███████╗██████╗
 ██║   ██║██║██╔══██╗██╔════╝██╔══██╗
 ██║   ██║██║██████╔╝█████╗  ██████╔╝
 ╚██╗ ██╔╝██║██╔═══╝ ██╔══╝  ██╔══██╗
  ╚████╔╝ ██║██║     ███████╗██║  ██║
   ╚═══╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝
```

---

## What it does

Viper is a ReAct agent — it reasons, calls tools, observes results, and loops until it has an answer. Give it a task and it figures out the tool sequence itself.

- **Thinks before acting** — visible reasoning blocks before every tool call
- **Writes and runs code** — Python or shell, to solve anything no tool covers
- **Controls a browser** — headless for scraping, visible when it hits CAPTCHA/2FA
- **Remembers your session** — auto-saves on exit, `--resume` to continue
- **Runs on any provider** — Ollama locally, or OpenRouter / HuggingFace / Anthropic via API key

---

## Quick start

**Mac / Linux**
```bash
curl -fsSL https://raw.githubusercontent.com/arpitmishra61/viper/main/install.sh | bash
```

**Windows (PowerShell)**
```powershell
iwr -useb https://raw.githubusercontent.com/arpitmishra61/viper/main/install.ps1 | iex
```

Then run `viper`. The first run asks you to **Sign up** or **Sign in** (your browser opens a short form),
or to continue free for 3 queries. After that you pick a provider with the arrow keys and paste your own API key.

```bash
viper            # first run: sign up / sign in, pick a provider, paste your key
viper login      # sign up or sign in again
viper logout     # sign out and remove saved keys
```

Skip the prompts by exporting a key instead: `export OPENROUTER_API_KEY="sk-or-..."`.
Maintainers: backend and auth page setup is in [supabase/README.md](supabase/README.md).

**Manual install**
```bash
pip install "viper-agent[all]"
playwright install chromium
viper
```

---

## Providers

| Provider | Requires | Default model |
|---|---|---|
| `ollama` | Local Ollama running | llama3.2 |
| `openrouter` | `OPENROUTER_API_KEY` | openrouter/free (free tier) |
| `huggingface` | `HF_TOKEN` | Llama-3.1-8B-Instruct |
| `anthropic` | `OPENROUTER_API_KEY` | claude-3.5-haiku |

```bash
python agent.py --provider ollama --model llama3.2
python agent.py --provider openrouter --model mistralai/mistral-7b-instruct:free
python agent.py --provider anthropic --model anthropic/claude-3.5-haiku
python agent.py --list-providers
```

Put your keys in a `.env` file at the project root — loaded automatically:

```
OPENROUTER_API_KEY=sk-or-...
HF_TOKEN=hf_...
```

---

## Tools (11)

| Tool | What it does |
|---|---|
| `think` | Reason step-by-step before acting |
| `web_search` | DuckDuckGo — free, no API key |
| `http_fetch` | Lightweight HTTP client for REST APIs |
| `browser` | Headless Chromium — navigate, click, fill, scrape |
| `run_shell` | Any terminal command — git, npm, docker, etc. |
| `run_python` | Write and execute Python on the fly |
| `calculate` | Safe AST-based math (no eval risk) |
| `read_file` | Read local text files |
| `write_file` | Create or overwrite files |
| `edit_file` | Surgical find-and-replace in files |
| `list_dir` | List files with glob pattern support |

### Browser: headless by default, visible when needed

```
scraping / analysis      →  headless (background, no window)
CAPTCHA / 2FA / OAuth    →  visible window + waits for you to complete it
payment confirmation      →  visible window + waits for you to confirm
bot-detection blocked     →  visible window (real Chrome context)
```

The agent switches modes automatically when it detects an auth wall or CAPTCHA in the page text.

### Shell: persistent working directory

`cd` commands persist across calls — the agent navigates your filesystem like a real shell session.

---

## Slash commands

```
/think      toggle visible thinking blocks on/off
/clear      wipe conversation history
/model      switch model mid-session:  /model anthropic/claude-3.5-haiku
/tools      list all tools
/save       save session to JSON
/cwd        show or change working directory
/tokens     show context usage (tokens · chars · messages · % of limit)
/help       show all commands
```

---

## Flags

```bash
python agent.py --resume              # continue last saved session
python agent.py --cwd /path/to/repo  # start in a specific directory
python agent.py --api-key sk-...     # override API key inline
```

---

## Architecture

```
user input
  ↓
append to messages  →  prune if over ~25k tokens
  ↓
stream API response
  ↓
tool calls?
  ├─ think calls  →  display reasoning block  →  run serially
  ├─ action calls →  dangerous-check          →  run in parallel
  └─ truncate results to 8k chars each
  ↓
loop (max 20 rounds)  →  final answer  →  show token count
```

One OpenAI SDK client works across every provider — same request shape, different `base_url`. No vendor lock-in, no LangChain, no framework.

---

## Requirements

```
Python 3.11+
openai >= 1.50
rich >= 13.7
ddgs >= 0.1
playwright >= 1.40
python-dotenv >= 1.0
```

---

## Project structure

```
agent.py          main loop, streaming, slash commands, session persistence
providers.py      provider configs
system_prompt.py  Viper persona and tool guidance
tools/
  __init__.py     assembles TOOL_DEFINITIONS, routes execute_tool()
  state.py        shared working directory state
  think.py        think tool + verbose toggle
  search.py       web_search
  calculator.py   calculate
  files.py        read_file, write_file, edit_file, list_dir
  shell.py        run_shell (cd-aware)
  python_runner.py run_python
  browser.py      browser (Playwright, dual-mode)
  http.py         http_fetch
```

Adding a tool: create `tools/mytool.py` with the function + `DEFINITIONS` list, add one import line to `tools/__init__.py`. Done.
