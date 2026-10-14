"""Security guardrails: PII detection, prompt-injection detection, path/URL safety."""

import ipaddress
import re
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


def check_path(path_str: str) -> str | None:
    """Return an error string if path is blocked (traversal or sensitive); None if safe."""
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
    if resolved.name in _SENSITIVE_FILENAMES:
        return f"Blocked: access to sensitive file '{resolved.name}'"

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


def scan_python_code(code: str) -> list[str]:
    """Return list of dangerous pattern descriptions found in code (empty = clean)."""
    return [desc for pat, desc in _DANGEROUS_PY if pat.search(code)]
