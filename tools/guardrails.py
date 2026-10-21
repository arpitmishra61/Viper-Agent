"""Security guardrails: PII detection, prompt-injection detection, path/URL safety."""

import ast
import fnmatch
import ipaddress
import re
import threading
import socket as _socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse as _urlparse

# ── PII patterns ───────────────────────────────────────────────────────────────
# Each entry: display_name → compiled regex
_PII_PATTERNS: dict[str, re.Pattern] = {
    "SSN":         re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    "Credit Card": re.compile(r'\b(?:\d{4}[- ]){3}\d{4}\b'),
    "Phone":       re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),
    "Email":       re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    "API Key":     re.compile(
        r'\b(?:sk-[a-zA-Z0-9]{20,}'           # OpenAI-style
        r'|sk-or-v1-[a-zA-Z0-9]{40,}'         # OpenRouter
        r'|ghp_[a-zA-Z0-9]{36}'               # GitHub PAT
        r'|AIza[0-9A-Za-z\-_]{35}'            # Google API key
        r'|xox[bpoa]-[0-9A-Za-z\-]{10,})\b'  # Slack token
    ),
    "Password":    re.compile(r'(?i)(?:password|passwd|pwd)\s*[=:]\s*\S+'),
}

# ── Prompt-injection patterns ──────────────────────────────────────────────────
# Each entry: (compiled regex, human-readable category)
_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'(?i)\bignore\b.{0,40}\b(?:previous|prior|above|all)\b.{0,40}\binstructions?\b'),
     "instruction override"),
    (re.compile(r'(?i)\bdisregard\b.{0,40}\b(?:previous|prior|above|system)\b'),
     "instruction override"),
    (re.compile(r'(?i)\byou\s+are\s+now\s+(?:a|an)\b'),
     "persona hijack"),
    (re.compile(r'(?i)\bnew\s+(?:system\s+)?instructions?\s*:'),
     "instruction injection"),
    (re.compile(r'(?i)\bsystem\s+(?:override|prompt)\b'),
     "system override"),
    (re.compile(r'(?i)\bforget\s+(?:everything|all\s+(?:you\s+know|prior|previous))\b'),
     "context wipe"),
    (re.compile(r'(?i)\b(?:exfiltrate|secretly\s+send|leak)\b.{0,40}\b(?:files?|data|credentials?|key)\b'),
     "data exfiltration"),
    (re.compile(r'(?i)\bprint\s+(?:your\s+)?(?:system\s+prompt|instructions?|api\s+key)\b'),
     "secret extraction"),
    (re.compile(r'(?i)\bact\s+as\s+(?:an?\s+)?(?:jailbreak|dan|unrestricted|evil)\b'),
     "jailbreak attempt"),
    (re.compile(r'<\|(?:system|user|assistant|im_start|im_end)\|>'),
     "token injection"),
    (re.compile(r'\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>'),
     "template injection"),
    (re.compile(r'(?i)###\s*(?:system|instruction)\b'),
     "markdown injection"),
]

# Tools whose output originates outside the agent (external/untrusted)
EXTERNAL_TOOLS: frozenset[str] = frozenset({
    "web_search", "browser", "http_fetch",
    "read_file", "run_shell", "run_python",
    "write_file", "edit_file", "list_dir",
})


@dataclass
class PIIResult:
    found: list[str] = field(default_factory=list)

    @property
    def has_pii(self) -> bool:
        return bool(self.found)


@dataclass
class InjectionResult:
    suspicious: bool = False
    matches: list[tuple[str, str]] = field(default_factory=list)  # (category, snippet)


def detect_pii(text: str) -> PIIResult:
    """Return names of PII types found in text (no redaction)."""
    return PIIResult(found=[name for name, pat in _PII_PATTERNS.items() if pat.search(text)])


def detect_injection(text: str) -> InjectionResult:
    """Scan text for prompt-injection patterns."""
    matches: list[tuple[str, str]] = []
    for pat, category in _INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            matches.append((category, m.group(0)[:80]))
    return InjectionResult(suspicious=bool(matches), matches=matches)


def tag_external(tool_name: str, content: str) -> str:
    """Wrap tool output in an untrusted-content envelope so the model knows the source."""
    return (
        f'<tool_result name="{tool_name}" trust="untrusted">\n'
        f"{content}\n"
        f"</tool_result>"
    )


# ── Path security ──────────────────────────────────────────────────────────────

_SENSITIVE_PATH_PREFIXES: list[str] = [
    str(Path.home() / ".ssh"),
    str(Path.home() / ".aws"),
    str(Path.home() / ".gnupg"),
    str(Path.home() / ".viper" / ".key"),
    # Windows credential stores
    str(Path.home() / "AppData" / "Roaming" / "Microsoft" / "Credentials"),
]

_SENSITIVE_FILENAMES: frozenset[str] = frozenset({
    ".env", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa",
    "authorized_keys", "known_hosts", "credentials",
})


# ── .env access (human-in-the-loop) ───────────────────────────────────────────
# Reading a .env file is sometimes legitimate ("is OPENAI_API_KEY set?"), so read_file may open
# one — but only after the user approves it at a prompt. Approval is NOT a tool argument (any
# code path that calls tools directly could forge that); it is a one-time grant that only the
# confirmation prompt in agent.py can create. Without a grant, read_file refuses.

_ENV_FILE_RE = re.compile(r"^\.env(?:\.[\w.-]+)?$", re.I)
_ENV_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template", "dist"})


def is_env_file(path_str) -> bool:
    """True for .env and .env.<anything> (secrets), False for .env.example-style templates."""
    try:
        name = Path(path_str).expanduser().name
    except Exception:
        return False
    if not _ENV_FILE_RE.match(name):
        return False
    return name.rsplit(".", 1)[-1].lower() not in _ENV_TEMPLATE_SUFFIXES


_env_grants: dict[str, list[str]] = {}
_env_lock = threading.Lock()


def env_path_key(path_str) -> str:
    try:
        return str(Path(path_str).expanduser().resolve()).lower()
    except Exception:
        return str(path_str).lower()


def grant_env_access(path_str: str, mode: str) -> None:
    """Record a one-time approval. mode: 'full' (real values) or 'masked' (key names only)."""
    if mode not in ("full", "masked"):
        raise ValueError(f"unknown env access mode: {mode!r}")
    with _env_lock:
        _env_grants.setdefault(env_path_key(path_str), []).append(mode)


def take_env_grant(path_str: str) -> str | None:
    """Consume one approval for this path; None if there isn't one."""
    key = env_path_key(path_str)
    with _env_lock:
        modes = _env_grants.get(key)
        if not modes:
            return None
        mode = modes.pop(0)
        if not modes:
            del _env_grants[key]
        return mode


_ENV_LINE_RE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z_][\w.-]*\s*=\s*)(.*)$")
_PLACEHOLDER_RE = re.compile(r"(?i)^(?:your[_-]|change[_-]?me|x{3,}$|todo|placeholder|<.*>$|\.\.\.$)")


def _mask_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    if not v:
        return "<empty>"
    if _PLACEHOLDER_RE.match(v):
        return f"<placeholder-looking, {len(v)} chars>"
    return f"<set, {len(v)} chars>"


def mask_env(text: str) -> str:
    """Hide every value in a .env file; keep key names, comments and structure.

    Answers "which keys exist / are they empty or placeholders" without exposing a secret.
    Anything that can't be parsed as KEY=value is redacted rather than guessed at.
    """
    out: list[str] = []
    open_quote: str | None = None            # inside a multi-line quoted value
    for line in text.splitlines():
        if open_quote:
            if line.rstrip().endswith(open_quote):
                open_quote = None
            continue
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if stripped.startswith("#"):
            out.append("# <comment redacted>" if detect_pii(line).has_pii else line)
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            out.append("<unparsed line redacted>")
            continue
        prefix, raw = m.groups()
        v = raw.strip()
        if v[:1] in ("'", '"') and not (len(v) >= 2 and v.endswith(v[0])):
            open_quote = v[0]
            out.append(prefix + "<set, multi-line>")
            continue
        out.append(prefix + _mask_value(raw))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def check_path(path_str: str, allow_env: bool = False) -> str | None:
    """Return an error string if path is blocked (traversal or sensitive); None if safe.

    allow_env: skip only the .env filename block (the caller holds a user approval for it).
    Traversal and sensitive-directory checks always apply.
    """
    try:
        raw = Path(path_str).expanduser()
    except Exception as e:
        return f"Invalid path: {e}"

    # Block path traversal before any resolution
    if ".." in raw.parts:
        return f"Path traversal blocked: {path_str}"

    try:
        resolved = raw.resolve()
    except Exception:
        resolved = raw

    resolved_str = str(resolved).replace("\\", "/")

    # Sensitive prefix check (platform-normalised)
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved_str.lower().startswith(prefix.replace("\\", "/").lower()):
            return f"Blocked: access to sensitive path '{resolved_str}'"

    # Sensitive filename check
    if resolved.name in _SENSITIVE_FILENAMES and not (allow_env and is_env_file(path_str)):
        return f"Blocked: access to sensitive file '{resolved.name}'"

    return None


# ── Shell command path security ───────────────────────────────────────────────
# check_path() only protects the file tools. A model that is refused by read_file can
# simply `type .env` through run_shell, so commands get the same treatment.
#
# This is a heuristic on the command text, not a sandbox: it catches the direct and the
# lightly-obfuscated ways of naming these files. Anything built at runtime (string
# concatenation, variables, encoded payloads) or a bare `*` glob in cmd.exe is not caught.

_SHELL_NAMES = sorted(n for n in _SENSITIVE_FILENAMES if n not in (".env", "credentials"))
_SHELL_NAME_ALT = "|".join(re.escape(n) for n in _SHELL_NAMES)

# .env and .env.local-style files, but not the usual committed templates
_SHELL_ENV_RE = re.compile(r"(?<![\w.-])\.env(?![\w-])(?!\.(?:example|sample|template|dist)\b)", re.I)

_SHELL_SENSITIVE_RES: list[re.Pattern] = [
    # key / credential files by name (public keys excluded)
    re.compile(rf"(?<![\w-])(?:{_SHELL_NAME_ALT})(?![\w-])(?!\.pub\b)", re.I),
    # sensitive directories
    re.compile(r"(?<![\w.-])\.(?:ssh|aws|gnupg)(?![\w.-])", re.I),
    re.compile(r"(?<![\w-])\.viper/\.key\b", re.I),
    re.compile(r"microsoft/credentials\b", re.I),
]

# Names a wildcard could expand to (`cat .e*`, `type id_*`)
_GLOB_TARGETS: list[str] = [*_SHELL_NAMES, ".env", ".ssh", ".aws", ".gnupg"]


def _glob_hit(command: str) -> str | None:
    for token in re.split(r"[\s|&;<>()\"']+", command):
        if not any(c in token for c in "*?["):
            continue
        base = token.rsplit("/", 1)[-1].lower()
        if len(re.sub(r"[*?\[\]]", "", base)) < 2:      # `*`, `?`, `*e*` are too broad to treat as a probe
            continue
        for target in _GLOB_TARGETS:
            if target.startswith(".") and not base.startswith("."):
                continue                                 # shells don't expand * to dotfiles
            if fnmatch.fnmatchcase(target.lower(), base):
                return token
    return None


def check_command(command: str) -> str | None:
    """Return an error string if a shell command names a sensitive file/dir; None if it looks safe."""
    cmd = command.replace("\\", "/")
    m = _SHELL_ENV_RE.search(cmd)
    if m:
        return (
            f"Blocked: command references sensitive file '{m.group(0).strip()}'. Shell access to .env "
            "files is not allowed — to inspect one, call read_file on it; the user will be asked to approve."
        )
    for pat in _SHELL_SENSITIVE_RES:
        m = pat.search(cmd)
        if m:
            return f"Blocked: command references sensitive file or directory '{m.group(0).strip()}'"
    hit = _glob_hit(cmd)
    if hit:
        return f"Blocked: wildcard '{hit}' could expand to a sensitive file"
    return None


# ── URL / SSRF security ────────────────────────────────────────────────────────

_PRIVATE_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local / cloud metadata endpoints
    ipaddress.ip_network("100.64.0.0/10"),     # shared address space (RFC 6598)
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
]

_URL_BLOCKLIST: list[re.Pattern] = [
    re.compile(r'169\.254\.169\.254'),          # AWS / GCP / Azure metadata
    re.compile(r'metadata\.google\.internal'),
    re.compile(r'(?i)\.ngrok\.io(/|$)'),
    re.compile(r'(?i)ngrok-free\.app'),
    re.compile(r'(?i)webhook\.site'),
    re.compile(r'(?i)requestbin\.(net|com)'),
    re.compile(r'(?i)burpcollaborator\.net'),
    re.compile(r'(?i)interactsh\.com'),         # common OOB exfil testing domain
]


def check_url(url: str) -> str | None:
    """Return an error string if URL is blocked (denylist or SSRF); None if safe."""
    if not url:
        return None

    # Static denylist — no DNS needed
    for pat in _URL_BLOCKLIST:
        if pat.search(url):
            return f"URL blocked by denylist: {url}"

    # Parse URL
    try:
        parsed = _urlparse(url)
        host = parsed.hostname
        scheme = parsed.scheme.lower()
    except Exception:
        return None  # let the tool's own error handling deal with malformed URLs

    # Block non-HTTP(S) schemes before any host check (file:// has no host)
    if scheme and scheme not in ("http", "https"):
        return f"URL scheme '{scheme}' not allowed — only http/https"

    if not host:
        return None

    # SSRF: resolve hostname and reject private / loopback IPs
    try:
        addr_infos = _socket.getaddrinfo(host, None, proto=_socket.IPPROTO_TCP)
        for _fam, _typ, _proto, _cname, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            for net in _PRIVATE_NETWORKS:
                if ip in net:
                    return (
                        f"SSRF blocked: '{host}' resolves to private IP {sockaddr[0]}"
                    )
    except (_socket.gaierror, ValueError):
        pass  # unresolvable hostname — let the tool fail naturally

    return None


# ── Python code static analysis ───────────────────────────────────────────────

_DANGEROUS_PY: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bctypes\b'),
     "ctypes (direct memory/OS access)"),
    (re.compile(r'\bexec\s*\(\s*(?:base64|b64decode|bytes\.fromhex|bytearray)'),
     "exec() with encoded payload (obfuscated execution)"),
    (re.compile(r'\beval\s*\(\s*(?:base64|b64decode|bytes\.fromhex|bytearray)'),
     "eval() with encoded payload (obfuscated execution)"),
    (re.compile(r'\b__import__\s*\('),
     "__import__() dynamic import bypass"),
    (re.compile(r'(?i)import\s+winreg\b'),
     "winreg (Windows Registry access)"),
]


def _scan_python_env_access(code: str) -> list[str]:
    """AST-scan for open('.env') and Path('.env').read_* in Python code.

    This closes the gap where check_path/check_command protect the file tools and
    shell tool but run_python can still reach .env via the stdlib open() or pathlib.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # syntax errors are the runtime's problem

    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # open('.env', ...) or open(".env.local", ...)
        if isinstance(func, ast.Name) and func.id == "open" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if is_env_file(arg.value):
                    findings.append(f"open('{arg.value}') reads a secrets file")

        # pathlib.Path('.env').read_text() / .read_bytes() / .open()
        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "read_bytes", "open"):
            inner = func.value
            if isinstance(inner, ast.Call) and inner.args:
                inner_arg = inner.args[0]
                if isinstance(inner_arg, ast.Constant) and isinstance(inner_arg.value, str):
                    if is_env_file(inner_arg.value):
                        findings.append(
                            f"Path('{inner_arg.value}').{func.attr}() reads a secrets file"
                        )

    return findings


def scan_python_code(code: str) -> list[str]:
    """Return list of dangerous pattern descriptions found in code (empty = clean)."""
    findings = [desc for pat, desc in _DANGEROUS_PY if pat.search(code)]
    findings.extend(_scan_python_env_access(code))
    return findings
