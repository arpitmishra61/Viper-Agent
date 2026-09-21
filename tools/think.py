_verbose = True


def is_verbose() -> bool:
    return _verbose


def set_verbose(v: bool) -> None:
    global _verbose
    _verbose = v


def think(thought: str) -> str:
    """Echoes the thought back into context. Display is handled by the agent loop."""
    return thought


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "think",
            "description": (
                "Think through a problem before acting. Call this to reason, plan, or check assumptions "
                "before using other tools or answering. Use it liberally — good thinking leads to better actions.\n\n"
                "Good uses:\n"
                "- Plan a sequence of tool calls before starting\n"
                "- Evaluate whether the last tool result makes sense\n"
                "- Reason about ambiguous user intent\n"
                "- Decide between multiple approaches\n"
                "- Check if you have all the information you need\n"
                "- Reflect on an error and figure out what to try next\n\n"
                "The thought is returned into your context, so it directly informs your next action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {
                        "type": "string",
                        "description": (
                            "Your reasoning, plan, or analysis. Be thorough and specific. "
                            "Think step by step. This is your scratchpad."
                        ),
                    }
                },
                "required": ["thought"],
            },
        },
    }
]
