"""GitHub tools — interact with GitHub repos, issues, and PRs via the REST API.

Token lifecycle:
  1. Read from GITHUB_TOKEN env var on first use.
  2. Set interactively with github_set_token (model asks the user when no token is found).

Cloning always runs in the E2B sandbox — untrusted repo code never touches the local machine.
"""

import base64
import json
import os
import urllib.error
import urllib.request

_GH_TOKEN: str | None = None
_API_BASE = "https://api.github.com"
_TIMEOUT = 20


# ── token management ───────────────────────────────────────────────────────────

def _get_token() -> str | None:
    global _GH_TOKEN
    if _GH_TOKEN:
        return _GH_TOKEN
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        _GH_TOKEN = env
    return _GH_TOKEN


def _require_token() -> str:
    tok = _get_token()
    if not tok:
        raise RuntimeError(
            "GitHub token not set. Call github_set_token with a Personal Access Token (PAT), "
            "or set the GITHUB_TOKEN environment variable. "
            "Create a PAT at https://github.com/settings/tokens — "
            "scopes needed: repo (for private repos) or public_repo (for public repos)."
        )
    return tok


def _api(method: str, path: str, body: dict = None):
    tok = _require_token()
    url = f"{_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "viper-agent/1.0",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read(4096).decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw).get("message", raw)
        except Exception:
            msg = raw
        raise RuntimeError(f"GitHub API {e.code}: {msg}")


# ── tool functions ─────────────────────────────────────────────────────────────

def github_set_token(token: str) -> str:
    """Store a GitHub PAT for this session."""
    global _GH_TOKEN
    token = token.strip()
    if not token:
        return "Error: token cannot be empty."
    _GH_TOKEN = token
    # Quick validation — fetch authenticated user
    try:
        me = _api("GET", "/user")
        return f"GitHub token accepted. Authenticated as: {me['login']}"
    except RuntimeError as e:
        _GH_TOKEN = None
        return f"Token rejected by GitHub: {e}"


def github_get_repo(owner: str, repo: str) -> str:
    """Get metadata about a GitHub repository."""
    try:
        r = _api("GET", f"/repos/{owner}/{repo}")
        return json.dumps({
            "full_name": r["full_name"],
            "description": r.get("description"),
            "default_branch": r["default_branch"],
            "open_issues": r["open_issues_count"],
            "stars": r["stargazers_count"],
            "forks": r["forks_count"],
            "language": r.get("language"),
            "url": r["html_url"],
        }, indent=2)
    except RuntimeError as e:
        return str(e)


def github_list_issues(owner: str, repo: str, state: str = "open",
                       labels: str = "", limit: int = 20) -> str:
    """List issues in a repository (excludes pull requests)."""
    try:
        qs = f"state={state}&per_page={min(limit, 100)}"
        if labels:
            qs += f"&labels={labels}"
        items = _api("GET", f"/repos/{owner}/{repo}/issues?{qs}")
        out = [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "labels": [lb["name"] for lb in i.get("labels", [])],
                "created_at": i["created_at"],
                "url": i["html_url"],
            }
            for i in (items if isinstance(items, list) else [])
            if "pull_request" not in i  # issues endpoint returns PRs too
        ]
        return json.dumps(out, indent=2)
    except RuntimeError as e:
        return str(e)


def github_get_issue(owner: str, repo: str, number: int) -> str:
    """Get a specific issue with its body and all comments."""
    try:
        i = _api("GET", f"/repos/{owner}/{repo}/issues/{number}")
        comments_raw = _api("GET", f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=50")
        return json.dumps({
            "number": i["number"],
            "title": i["title"],
            "state": i["state"],
            "body": i.get("body") or "",
            "labels": [lb["name"] for lb in i.get("labels", [])],
            "author": i["user"]["login"],
            "created_at": i["created_at"],
            "url": i["html_url"],
            "comments": [
                {
                    "author": c["user"]["login"],
                    "body": c["body"],
                    "created_at": c["created_at"],
                }
                for c in (comments_raw if isinstance(comments_raw, list) else [])
            ],
        }, indent=2)
    except RuntimeError as e:
        return str(e)


def github_comment_issue(owner: str, repo: str, number: int, body: str) -> str:
    """Post a comment on an issue or pull request."""
    try:
        r = _api("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})
        return f"Comment posted: {r['html_url']}"
    except RuntimeError as e:
        return str(e)


def github_get_file(owner: str, repo: str, path: str, ref: str = "") -> str:
    """Read a file (or list a directory) from a GitHub repository."""
    try:
        endpoint = f"/repos/{owner}/{repo}/contents/{path}"
        if ref:
            endpoint += f"?ref={ref}"
        r = _api("GET", endpoint)
        if isinstance(r, list):
            # Directory listing
            entries = [
                {"name": f["name"], "type": f["type"], "size": f.get("size", 0)}
                for f in r
            ]
            return json.dumps(entries, indent=2)
        # File
        content = base64.b64decode(r["content"].replace("\n", "")).decode("utf-8", errors="replace")
        return f"SHA: {r['sha']}\nPath: {r['path']}\nSize: {r['size']} bytes\n\n{content}"
    except RuntimeError as e:
        return str(e)


def github_create_or_update_file(owner: str, repo: str, path: str, content: str,
                                  message: str, branch: str = "", sha: str = "") -> str:
    """Create or update a file in a GitHub repository via the Contents API.

    To UPDATE an existing file you must supply its current SHA (from github_get_file).
    Leave sha empty to CREATE a new file.
    """
    try:
        encoded = base64.b64encode(content.encode()).decode()
        body: dict = {"message": message, "content": encoded}
        if branch:
            body["branch"] = branch
        if sha:
            body["sha"] = sha  # required when updating
        r = _api("PUT", f"/repos/{owner}/{repo}/contents/{path}", body)
        action = "updated" if sha else "created"
        return (
            f"File {action}: {r['content']['html_url']}\n"
            f"Commit: {r['commit']['html_url']}\n"
            f"New SHA: {r['content']['sha']}"
        )
    except RuntimeError as e:
        return str(e)


def github_list_prs(owner: str, repo: str, state: str = "open", limit: int = 20) -> str:
    """List pull requests in a repository."""
    try:
        prs = _api("GET", f"/repos/{owner}/{repo}/pulls?state={state}&per_page={min(limit, 100)}")
        out = [
            {
                "number": p["number"],
                "title": p["title"],
                "state": p["state"],
                "head": p["head"]["ref"],
                "base": p["base"]["ref"],
                "draft": p.get("draft", False),
                "created_at": p["created_at"],
                "url": p["html_url"],
            }
            for p in (prs if isinstance(prs, list) else [])
        ]
        return json.dumps(out, indent=2)
    except RuntimeError as e:
        return str(e)


def github_create_pr(owner: str, repo: str, title: str, body: str,
                     head: str, base: str, draft: bool = False) -> str:
    """Create a pull request.

    head: the branch with your changes (e.g. 'feature/my-fix' or 'user:branch' for forks)
    base: the branch you want to merge into (e.g. 'main')
    """
    try:
        r = _api("POST", f"/repos/{owner}/{repo}/pulls", {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        })
        return (
            f"PR #{r['number']} created: {r['html_url']}\n"
            f"Title: {r['title']}\n"
            f"Head → Base: {r['head']['ref']} → {r['base']['ref']}"
        )
    except RuntimeError as e:
        return str(e)


def github_clone_repo(owner: str, repo: str, branch: str = "") -> str:
    """Clone a GitHub repository into the E2B sandbox.

    Always runs in the sandbox — repo code never executes on the local machine.
    The cloned repo lands at /home/user/<repo> inside the sandbox and is accessible
    to all subsequent sandboxed run_shell / run_python calls in this session.
    """
    from tools.sandbox import run_shell_in_sandbox
    tok = _get_token()
    if tok:
        # Embed token in URL so private repos work; masked in output
        clone_url = f"https://x-access-token:{tok}@github.com/{owner}/{repo}.git"
    else:
        clone_url = f"https://github.com/{owner}/{repo}.git"

    dest = f"/home/user/{repo}"
    cmd = f"git clone {clone_url}"
    if branch:
        cmd += f" --branch {branch} --single-branch"
    cmd += f" {dest}"

    result = run_shell_in_sandbox(cmd, timeout=120)

    # Never leak the token in tool output
    if tok:
        result = result.replace(tok, "***")

    return result + f"\nCloned to: {dest} (inside sandbox)"


# ── OpenAI tool definitions ────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "github_set_token",
            "description": (
                "Store a GitHub Personal Access Token (PAT) for this session. "
                "Call this first if the user has not set GITHUB_TOKEN in their environment. "
                "The token is kept in memory only — never written to disk."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "token": {
                        "type": "string",
                        "description": "GitHub PAT (classic or fine-grained). Scopes: repo or public_repo.",
                    },
                },
                "required": ["token"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_get_repo",
            "description": "Get metadata about a GitHub repository: description, default branch, stars, open issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner (user or org)"},
                    "repo":  {"type": "string", "description": "Repository name"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_issues",
            "description": "List issues in a repository. Pull requests are excluded automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":  {"type": "string", "description": "Repository owner"},
                    "repo":   {"type": "string", "description": "Repository name"},
                    "state":  {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "labels": {"type": "string", "description": "Comma-separated label names to filter by"},
                    "limit":  {"type": "integer", "description": "Max issues to return (default 20, max 100)", "default": 20},
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_get_issue",
            "description": "Get a specific issue with its full body and all comments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":  {"type": "string", "description": "Repository owner"},
                    "repo":   {"type": "string", "description": "Repository name"},
                    "number": {"type": "integer", "description": "Issue number"},
                },
                "required": ["owner", "repo", "number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_comment_issue",
            "description": "Post a comment on a GitHub issue or pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":  {"type": "string", "description": "Repository owner"},
                    "repo":   {"type": "string", "description": "Repository name"},
                    "number": {"type": "integer", "description": "Issue or PR number"},
                    "body":   {"type": "string", "description": "Comment text (Markdown supported)"},
                },
                "required": ["owner", "repo", "number", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_get_file",
            "description": (
                "Read a file from a GitHub repository, or list a directory. "
                "Returns file content plus its SHA (needed to update the file later)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo":  {"type": "string", "description": "Repository name"},
                    "path":  {"type": "string", "description": "File or directory path inside the repo"},
                    "ref":   {"type": "string", "description": "Branch, tag, or commit SHA (default: repo's default branch)"},
                },
                "required": ["owner", "repo", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_create_or_update_file",
            "description": (
                "Create or update a single file in a GitHub repository via the Contents API. "
                "To update an existing file you MUST supply its current SHA from github_get_file. "
                "This commits directly to the branch — no local clone required."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":   {"type": "string", "description": "Repository owner"},
                    "repo":    {"type": "string", "description": "Repository name"},
                    "path":    {"type": "string", "description": "File path inside the repo (e.g. 'src/main.py')"},
                    "content": {"type": "string", "description": "Full file content (plain text)"},
                    "message": {"type": "string", "description": "Commit message"},
                    "branch":  {"type": "string", "description": "Target branch (default: repo's default branch)"},
                    "sha":     {"type": "string", "description": "Current file SHA — required when updating an existing file"},
                },
                "required": ["owner", "repo", "path", "content", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_prs",
            "description": "List pull requests in a repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo":  {"type": "string", "description": "Repository name"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "limit": {"type": "integer", "description": "Max PRs to return (default 20, max 100)", "default": 20},
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_create_pr",
            "description": (
                "Create a pull request on GitHub. "
                "The head branch must already exist and have commits ahead of the base branch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo":  {"type": "string", "description": "Repository name"},
                    "title": {"type": "string", "description": "PR title"},
                    "body":  {"type": "string", "description": "PR description (Markdown supported)"},
                    "head":  {"type": "string", "description": "Branch with your changes (e.g. 'feature/fix-bug' or 'user:branch' for forks)"},
                    "base":  {"type": "string", "description": "Branch to merge into (e.g. 'main')"},
                    "draft": {"type": "boolean", "description": "Open as draft PR (default false)", "default": False},
                },
                "required": ["owner", "repo", "title", "body", "head", "base"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_clone_repo",
            "description": (
                "Clone a GitHub repository into the E2B sandbox. "
                "Always sandboxed — the repo lands at /home/user/<repo> inside the microVM "
                "and is accessible to subsequent run_shell(sandbox=true) calls. "
                "Use this before running, building, or modifying repo code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":  {"type": "string", "description": "Repository owner"},
                    "repo":   {"type": "string", "description": "Repository name"},
                    "branch": {"type": "string", "description": "Branch to clone (default: repo's default branch)"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
]
