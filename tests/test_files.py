"""Tests for tools/files.py — real file I/O in tmp dirs, plus guard integration."""

import pytest
from tools.files import read_file, write_file, edit_file, list_dir


# ── write_file / read_file ────────────────────────────────────────────────────

class TestWriteRead:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "a.txt"
        assert "Written" in write_file(str(p), "hello")
        assert read_file(str(p)) == "hello"

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "x" / "y" / "z.txt"
        write_file(str(p), "deep")
        assert p.read_text(encoding="utf-8") == "deep"

    def test_overwrite_replaces(self, tmp_path):
        p = tmp_path / "a.txt"
        write_file(str(p), "one")
        write_file(str(p), "two")
        assert read_file(str(p)) == "two"

    def test_append_mode(self, tmp_path):
        p = tmp_path / "a.txt"
        write_file(str(p), "one")
        write_file(str(p), "+two", mode="append")
        assert read_file(str(p)) == "one+two"

    def test_unicode(self, tmp_path):
        p = tmp_path / "u.txt"
        write_file(str(p), "héllo — 日本語")
        assert read_file(str(p)) == "héllo — 日本語"

    def test_read_missing(self, tmp_path):
        assert read_file(str(tmp_path / "nope.txt")).startswith("File not found")

    def test_read_directory(self, tmp_path):
        assert read_file(str(tmp_path)).startswith("Not a file")

    def test_read_too_large(self, tmp_path):
        p = tmp_path / "big.txt"
        p.write_text("x" * (60 * 1024), encoding="utf-8")
        assert "too large" in read_file(str(p)).lower()

    def test_read_binary(self, tmp_path):
        p = tmp_path / "b.bin"
        p.write_bytes(b"\xff\xfe\x00\x80")
        assert "binary" in read_file(str(p)).lower()


# ── edit_file ─────────────────────────────────────────────────────────────────

class TestEdit:
    def test_unique_replace(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("alpha beta gamma", encoding="utf-8")
        assert "Replaced 1" in edit_file(str(p), "beta", "BETA")
        assert p.read_text(encoding="utf-8") == "alpha BETA gamma"

    def test_string_not_found(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("alpha", encoding="utf-8")
        assert "not found" in edit_file(str(p), "zzz", "y").lower()
        assert p.read_text(encoding="utf-8") == "alpha"

    def test_ambiguous_match_rejected_and_file_untouched(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("foo foo", encoding="utf-8")
        assert "2 occurrences" in edit_file(str(p), "foo", "bar")
        assert p.read_text(encoding="utf-8") == "foo foo"

    def test_missing_file(self, tmp_path):
        assert edit_file(str(tmp_path / "nope.txt"), "a", "b").startswith("File not found")


# ── list_dir ──────────────────────────────────────────────────────────────────

class TestListDir:
    def test_lists_files_and_dirs(self, tmp_path):
        (tmp_path / "f.txt").write_text("x")
        (tmp_path / "sub").mkdir()
        out = list_dir(str(tmp_path))
        assert "f.txt" in out
        assert "sub/" in out        # directories carry a trailing slash

    def test_pattern_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.txt").write_text("x")
        out = list_dir(str(tmp_path), pattern="*.py")
        assert "a.py" in out
        assert "b.txt" not in out

    def test_non_recursive_hides_nested(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.txt").write_text("x")
        assert "deep.txt" not in list_dir(str(tmp_path))

    def test_recursive_finds_nested(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.txt").write_text("x")
        assert "deep.txt" in list_dir(str(tmp_path), recursive=True)

    def test_no_match(self, tmp_path):
        assert "No entries" in list_dir(str(tmp_path), pattern="*.zzz")

    def test_not_a_directory(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        assert list_dir(str(f)).startswith("Not a directory")

    def test_missing_path(self, tmp_path):
        assert list_dir(str(tmp_path / "nope")).startswith("Path not found")

    def test_truncates_at_500(self, tmp_path):
        for i in range(505):
            (tmp_path / f"f{i:03}.txt").write_text("")
        out = list_dir(str(tmp_path))
        assert "5 more entries truncated" in out


# ── guardrail integration: check_path must fire before any I/O ────────────────

class TestPathGuardIntegration:
    def test_read_traversal_blocked(self):
        assert "traversal" in read_file("../../etc/passwd").lower()

    def test_write_traversal_blocked(self):
        assert "traversal" in write_file("../evil.txt", "x").lower()

    def test_edit_traversal_blocked(self):
        assert "traversal" in edit_file("../evil.txt", "a", "b").lower()

    def test_list_traversal_blocked(self):
        assert "traversal" in list_dir("../..").lower()

    def test_read_existing_env_file_blocked_and_not_leaked(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("API_KEY=supersecret", encoding="utf-8")
        out = read_file(str(env))
        assert "Blocked" in out
        assert "supersecret" not in out

    def test_write_env_file_blocked_and_not_created(self, tmp_path):
        env = tmp_path / ".env"
        assert "Blocked" in write_file(str(env), "X=1")
        assert not env.exists()

    def test_edit_env_file_blocked_and_unchanged(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("X=1", encoding="utf-8")
        assert "Blocked" in edit_file(str(env), "1", "2")
        assert env.read_text(encoding="utf-8") == "X=1"

    @pytest.mark.parametrize("path", ["~/.ssh/id_rsa", "~/.aws/credentials", "~/.gnupg/pubring.kbx"])
    def test_sensitive_home_paths_blocked(self, path):
        assert "Blocked" in read_file(path)
