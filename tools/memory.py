"""Persistent long-term memory across sessions.

Stored as ~/.viper/memory.json — plain JSON, no encryption needed (user chose what to save).
"""
import json
import time
from pathlib import Path

_MEMORY_FILE = Path.home() / ".viper" / "memory.json"


def _load() -> dict:
    if not _MEMORY_FILE.exists():
        return {"entries": []}
    try:
        return json.loads(_MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"entries": []}


def _save(data: dict) -> None:
    _MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def save_memory(key: str, value: str) -> str:
    data = _load()
    for entry in data["entries"]:
        if entry["key"].lower() == key.lower():
            entry["value"] = value
            entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save(data)
            return f"Memory updated: [{key}] {value}"
    data["entries"].append(
        {"key": key, "value": value, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    )
    _save(data)
    return f"Memory saved: [{key}] {value}"


def list_memories() -> str:
    data = _load()
    if not data["entries"]:
        return "No memories saved yet."
    lines = []
    for e in data["entries"]:
        ts = e.get("updated_at") or e.get("created_at", "")
        lines.append(f"[{e['key']}] {e['value']}  ({ts})")
    return "\n".join(lines)


def delete_memory(key: str) -> str:
    data = _load()
    before = len(data["entries"])
    data["entries"] = [e for e in data["entries"] if e["key"].lower() != key.lower()]
    if len(data["entries"]) == before:
        return f"No memory found with key: {key}"
    _save(data)
    return f"Memory deleted: {key}"


def search_memory(query: str) -> str:
    data = _load()
    q = query.lower()
    matches = [
        e for e in data["entries"]
        if q in e["key"].lower() or q in e["value"].lower()
    ]
    if not matches:
        return f"No memories matching: {query}"
    return "\n".join(f"[{e['key']}] {e['value']}" for e in matches)


def load_memory_context() -> str:
    """Return memories formatted for system prompt injection. Empty string if none."""
    data = _load()
    if not data["entries"]:
        return ""
    lines = [f"- [{e['key']}] {e['value']}" for e in data["entries"]]
    return (
        "\n\n## Persistent Memory\n"
        "Facts and preferences the user has asked you to remember across sessions:\n"
        + "\n".join(lines)
        + "\nApply these automatically — do not repeat them back unless asked."
    )


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Save a fact or preference to long-term memory that persists across sessions. "
                "Call when the user says 'remember that...', 'always use...', 'my project is...', "
                "or shares any preference they would want recalled in a future conversation. "
                "Key: short snake_case identifier. Saving with an existing key updates it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short snake_case identifier (e.g. project_stack, preferred_language)",
                    },
                    "value": {
                        "type": "string",
                        "description": "The fact or preference to remember (be specific and complete)",
                    },
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "List all saved long-term memories. Call when the user asks what you remember about them.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": "Delete a saved memory by key. Call when the user says 'forget that...' or 'stop remembering...'",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Exact key of the memory to delete"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search saved memories by keyword. Use when a task might relate to something the user asked you to remember.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to search for"},
                },
                "required": ["query"],
            },
        },
    },
]
