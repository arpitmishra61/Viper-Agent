"""Tests for pure utility functions in agent.py.

Requires all Viper dependencies to be installed (rich, openai, ddgs, etc.)
since importing agent.py pulls them all in.
Run from terminal-agent/ or with PYTHONPATH=terminal-agent.
"""

import pytest
import agent as _agent

_context_chars     = _agent._context_chars
_estimate_tokens   = _agent._estimate_tokens
_prune_context     = _agent._prune_context
_truncate_result   = _agent._truncate_result
MAX_CONTEXT_CHARS  = _agent._MAX_CONTEXT_CHARS
MAX_TOOL_CHARS     = _agent._MAX_TOOL_OUTPUT_CHARS


# ── helpers ───────────────────────────────────────────────────────────────────

def _msgs(n: int, chars_each: int) -> list:
    """Build a message list: one system msg + n alternating user/assistant msgs."""
    out = [{"role": "system", "content": "system prompt"}]
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": "x" * chars_each})
    return out


# ── _context_chars ────────────────────────────────────────────────────────────

class TestContextChars:
    def test_empty_list(self):
        assert _context_chars([]) == 0

    def test_single_message(self):
        assert _context_chars([{"role": "user", "content": "hello"}]) == 5

    def test_none_content_counts_as_zero(self):
        assert _context_chars([{"role": "assistant", "content": None}]) == 0

    def test_multiple_messages_summed(self):
        msgs = [
            {"role": "user",      "content": "aaa"},   # 3
            {"role": "assistant", "content": "bb"},     # 2
        ]
        assert _context_chars(msgs) == 5

    def test_tool_call_content(self):
        # tool_calls messages have content=None; should not crash
        msgs = [{"role": "assistant", "content": None, "tool_calls": []}]
        assert _context_chars(msgs) == 0


# ── _estimate_tokens ──────────────────────────────────────────────────────────

class TestEstimateTokens:
    def test_approximation_is_chars_over_4(self):
        msgs = [{"role": "user", "content": "a" * 400}]
        assert _estimate_tokens(msgs) == 100

    def test_empty_is_zero(self):
        assert _estimate_tokens([]) == 0

    def test_multiple_messages(self):
        msgs = [
            {"role": "user",      "content": "a" * 200},
            {"role": "assistant", "content": "b" * 200},
        ]
        assert _estimate_tokens(msgs) == 100  # 400 / 4


# ── _prune_context ────────────────────────────────────────────────────────────

class TestPruneContext:
    def test_preserves_system_message(self):
        msgs = _msgs(50, 3_000)  # way over 100k limit
        pruned = _prune_context(msgs)
        assert pruned[0]["role"] == "system"

    def test_reduces_below_limit(self):
        msgs = _msgs(50, 3_000)
        pruned = _prune_context(msgs)
        assert _context_chars(pruned) <= MAX_CONTEXT_CHARS

    def test_does_not_prune_when_under_limit(self):
        msgs = _msgs(3, 100)
        original_len = len(msgs)
        pruned = _prune_context(msgs)
        assert len(pruned) == original_len

    def test_system_only_never_removed(self):
        # Even if a system message alone exceeds the limit it must stay
        msgs = [{"role": "system", "content": "x" * (MAX_CONTEXT_CHARS + 1)}]
        pruned = _prune_context(msgs)
        assert len(pruned) == 1
        assert pruned[0]["role"] == "system"

    def test_oldest_non_system_dropped_first(self):
        # Use a unique marker in the oldest non-system message so we can tell it was removed
        msgs = [{"role": "system", "content": "system prompt"}]
        msgs.append({"role": "user", "content": "OLDEST-" + "x" * 14_993})  # unique prefix
        for i in range(9):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({"role": role, "content": "x" * 15_000})
        pruned = _prune_context(msgs)
        remaining_contents = [m["content"] for m in pruned]
        assert not any("OLDEST-" in c for c in remaining_contents)


# ── _truncate_result ──────────────────────────────────────────────────────────

class TestTruncateResult:
    def test_short_result_unchanged(self):
        s = "hello world"
        assert _truncate_result(s) == s

    def test_exact_limit_unchanged(self):
        s = "x" * MAX_TOOL_CHARS
        assert _truncate_result(s) == s

    def test_over_limit_is_cut(self):
        s = "x" * (MAX_TOOL_CHARS + 500)
        result = _truncate_result(s)
        assert len(result) < len(s)

    def test_truncation_notice_appended(self):
        s = "x" * (MAX_TOOL_CHARS + 500)
        result = _truncate_result(s)
        assert "truncated" in result

    def test_truncated_result_starts_with_original(self):
        s = "hello " + "x" * MAX_TOOL_CHARS
        result = _truncate_result(s)
        assert result.startswith("hello ")
