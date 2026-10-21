"""Skills: on-demand instruction packs. The model sees a one-line index and loads a body via use_skill."""

import re
from dataclasses import dataclass
from pathlib import Path

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "skills"
USER_DIR = Path.home() / ".viper" / "skills"

# agent._truncate_result cuts tool output at 8k chars, so a skill must fit under that.
MAX_SKILL_CHARS = 7_500

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


def skill_dirs() -> list[Path]:
    # Never the working directory: a cloned repo could ship a skill that steers the model.
    return [BUNDLED_DIR, USER_DIR]


def is_skill_path(path_str: str) -> bool:
    try:
        resolved = Path(path_str).expanduser().resolve()
    except Exception:
        return False
    for d in skill_dirs():
        try:
            resolved.relative_to(d.resolve())
            return True
        except ValueError:
            continue
    return False


def _parse(text: str) -> tuple[dict[str, str], str]:
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            meta = {}
            for line in lines[1:end]:
                key, sep, value = line.partition(":")
                if sep:
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            return meta, "\n".join(lines[end + 1:]).lstrip("\n")
    return {}, text


def discover() -> dict[str, Skill]:
    """Later directories override earlier ones, so ~/.viper/skills can replace a bundled skill."""
    found: dict[str, Skill] = {}
    for d in skill_dirs():
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            md = sub / "SKILL.md"
            if not md.is_file() or not _NAME_RE.match(sub.name):
                continue
            try:
                meta, _ = _parse(md.read_text(encoding="utf-8"))
            except OSError:
                continue
            desc = meta.get("description", "").strip()
            if desc:
                found[sub.name] = Skill(sub.name, desc, md)
    return found


def index_text() -> str:
    skills = discover()
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in skills.values()]
    return (
        "\n\n## Skills\n"
        "Skills are task playbooks you load on demand. Before starting a task, check this list; if one "
        "matches, call `use_skill` with its name and follow it. Load only what the task needs.\n"
        + "\n".join(lines)
    )


def use_skill(name: str) -> str:
    name = (name or "").strip().lower()
    skills = discover()
    skill = skills.get(name)
    if skill is None:
        avail = ", ".join(skills) or "none"
        return f"Unknown skill: {name!r}. Available skills: {avail}"
    try:
        _, body = _parse(skill.path.read_text(encoding="utf-8"))
    except OSError as e:
        return f"Could not read skill {name!r}: {e}"
    if len(body) > MAX_SKILL_CHARS:
        body = body[:MAX_SKILL_CHARS] + "\n\n[skill truncated]"
    return f"# Skill: {skill.name}\n\n{body.strip()}"


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": (
                "Load a skill: a step-by-step playbook for a type of task. Skills are listed by name "
                "and description in the system prompt. Call this first when a listed skill matches the "
                "task, then follow the returned instructions. Returns the full skill text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name exactly as listed in the system prompt.",
                    }
                },
                "required": ["name"],
            },
        },
    }
]
