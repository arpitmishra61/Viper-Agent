from pathlib import Path

_MAX_READ_BYTES = 50 * 1024  # 50 KB


def read_file(path: str) -> str:
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"File not found: {path}"
        if not p.is_file():
            return f"Not a file: {path}"
        size = p.stat().st_size
        if size > _MAX_READ_BYTES:
            return f"File too large ({size // 1024} KB). Max is 50 KB."
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "Cannot read binary file — only text files are supported."
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(path: str, content: str, mode: str = "overwrite") -> str:
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        write_mode = "a" if mode == "append" else "w"
        with p.open(write_mode, encoding="utf-8") as f:
            f.write(content)
        size = p.stat().st_size
        return f"Written {size} bytes to {p}"
    except Exception as e:
        return f"Error writing file: {e}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"File not found: {path}"
        content = p.read_text(encoding="utf-8")
        count = content.count(old_string)
        if count == 0:
            return f"String not found in {path}"
        if count > 1:
            return f"Found {count} occurrences — provide more context to make it unique"
        p.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
        return f"Replaced 1 occurrence in {p}"
    except Exception as e:
        return f"Error editing file: {e}"


def list_dir(path: str = ".", pattern: str = "*", recursive: bool = False) -> str:
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Path not found: {path}"
        if not p.is_dir():
            return f"Not a directory: {path}"

        entries = sorted(p.rglob(pattern) if recursive else p.glob(pattern))

        if not entries:
            return f"No entries matching '{pattern}' in {p}"

        lines = []
        for e in entries[:500]:
            rel = e.relative_to(p)
            tag = "/" if e.is_dir() else ""
            lines.append(f"{rel}{tag}")

        result = "\n".join(lines)
        if len(entries) > 500:
            result += f"\n... ({len(entries) - 500} more entries truncated)"
        return f"{p}\n\n{result}"
    except Exception as e:
        return f"Error listing directory: {e}"


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text contents of a local file on the user's machine.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a file. Creates the file (and any parent directories) "
                "if it doesn't exist. Use mode='overwrite' to replace the whole file, "
                "'append' to add to the end."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "Write mode (default: overwrite)",
                        "default": "overwrite",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Surgically replace an exact string inside a file. "
                "The old_string must appear exactly once — provide enough surrounding context "
                "to make it unique if needed. Safer than rewriting the whole file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find and replace (must be unique in the file)",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Text to replace it with",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List files and directories. Supports glob patterns like '*.py' or '**/*.ts'. "
                "Use recursive=true to search through subdirectories. "
                "Use this before reading or editing to discover what files exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory to list (default: current directory)",
                        "default": ".",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to filter results (default: '*' = everything)",
                        "default": "*",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Search subdirectories recursively (default: false)",
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    },
]
