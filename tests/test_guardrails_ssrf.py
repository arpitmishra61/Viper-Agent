"""Extra edge cases for guardrails.check_url / check_path.

DNS is mocked where a hostname's resolution matters, so these run offline.
Literal IPs resolve without DNS and use the real resolver.
"""

import socket

import pytest
from tools import guardrails
from tools.guardrails import check_url, check_path


def fake_dns(monkeypatch, *ips):
    """Make every hostname resolve to the given IPs."""
    def _gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
    monkeypatch.setattr(guardrails._socket, "getaddrinfo", _gai)


# ── SSRF: private ranges ──────────────────────────────────────────────────────

class TestPrivateRanges:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/",
        "http://127.1.2.3:8080/admin",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://172.31.255.254/",
        "http://192.168.1.1/",
        "http://169.254.1.1/",
        "http://100.64.0.1/",
        "http://[::1]/",
    ])
    def test_literal_private_ips_blocked(self, url):
        result = check_url(url)
        assert result is not None and "SSRF" in result

    def test_localhost_blocked(self):
        assert check_url("http://localhost:11434/") is not None

    @pytest.mark.parametrize("ip", ["172.15.255.255", "172.32.0.1", "11.0.0.1", "8.8.8.8"])
    def test_just_outside_private_ranges_allowed(self, monkeypatch, ip):
        fake_dns(monkeypatch, ip)
        assert check_url("http://example.test/") is None


# ── SSRF: DNS-based ───────────────────────────────────────────────────────────

class TestDnsResolution:
    def test_public_name_resolving_to_private_ip_blocked(self, monkeypatch):
        fake_dns(monkeypatch, "10.1.2.3")
        result = check_url("https://innocent-looking.example/")
        assert result is not None and "10.1.2.3" in result

    def test_any_private_answer_blocks_even_with_public_ones(self, monkeypatch):
        fake_dns(monkeypatch, "8.8.8.8", "192.168.0.10")   # DNS-rebinding style mixed answer
        assert check_url("https://mixed.example/") is not None

    def test_public_resolution_allowed(self, monkeypatch):
        fake_dns(monkeypatch, "93.184.216.34")
        assert check_url("https://example.test/") is None

    def test_unresolvable_host_not_blocked(self, monkeypatch):
        def _fail(*a, **k):
            raise socket.gaierror("nope")
        monkeypatch.setattr(guardrails._socket, "getaddrinfo", _fail)
        assert check_url("https://does-not-exist.invalid/") is None


# ── denylist / scheme ─────────────────────────────────────────────────────────

class TestDenylistAndScheme:
    @pytest.mark.parametrize("url", [
        "http://metadata.google.internal/computeMetadata/v1/",
        "https://foo.interactsh.com/x",
        "HTTPS://WEBHOOK.SITE/abc",          # case-insensitive
        "https://abc.NGROK.IO/hook",
    ])
    def test_denylisted(self, url):
        assert check_url(url) is not None

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,hi", "gopher://x/", "file:///C:/Windows/win.ini"])
    def test_non_http_schemes_blocked(self, url):
        assert "scheme" in (check_url(url) or "").lower()

    def test_metadata_ip_blocked_even_in_query_string(self):
        assert check_url("https://example.com/redirect?to=169.254.169.254") is not None


# ── known gaps ────────────────────────────────────────────────────────────────

class TestKnownGaps:
    @pytest.mark.xfail(strict=True, reason="IPv4-mapped IPv6 (::ffff:127.0.0.1) isn't matched by the IPv4 networks")
    def test_ipv4_mapped_ipv6_loopback(self):
        assert check_url("http://[::ffff:127.0.0.1]/") is not None


# ── check_path extras ─────────────────────────────────────────────────────────

class TestCheckPathExtras:
    @pytest.mark.parametrize("name", ["id_ed25519", "id_ecdsa", "known_hosts", "credentials", ".netrc"])
    def test_sensitive_filenames_anywhere(self, tmp_path, name):
        assert check_path(str(tmp_path / name)) is not None

    def test_gnupg_blocked(self):
        assert check_path("~/.gnupg/secring.gpg") is not None

    def test_viper_key_blocked(self):
        assert check_path("~/.viper/.key") is not None

    def test_viper_session_dir_not_blocked(self):
        # only the key file is protected, not the whole ~/.viper directory
        assert check_path("~/.viper/sessions/x.viper") is None

    def test_dotdot_inside_filename_is_not_traversal(self, tmp_path):
        assert check_path(str(tmp_path / "notes..txt")) is None

    def test_similar_filename_not_blocked(self, tmp_path):
        assert check_path(str(tmp_path / "env.txt")) is None
        assert check_path(str(tmp_path / "my.env.example")) is None

    def test_empty_path(self):
        # resolves to the cwd — must not crash
        check_path("")
