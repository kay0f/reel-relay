"""Tests for the Instagram DM webhook: signature verification + reel extraction."""
import hashlib
import hmac

from server import config, main


def test_verify_signature_valid_and_invalid(monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "topsecret")
    body = b'{"object":"instagram"}'
    good = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert main._verify_meta_signature(body, good)
    assert not main._verify_meta_signature(body, "sha256=deadbeef")          # wrong digest
    assert not main._verify_meta_signature(body, good[len("sha256="):])       # missing prefix
    assert not main._verify_meta_signature(body, "")                          # missing header
    assert not main._verify_meta_signature(b'{"tampered":1}', good)           # body changed


def test_verify_signature_disabled_without_secret(monkeypatch):
    """With no app secret configured, every request must be rejected (webhook off)."""
    monkeypatch.setattr(config, "META_APP_SECRET", "")
    body = b"{}"
    sig = "sha256=" + hmac.new(b"anything", body, hashlib.sha256).hexdigest()
    assert not main._verify_meta_signature(body, sig)


def test_extract_ig_reel():
    payload = {
        "object": "instagram",
        "entry": [{"messaging": [{"message": {"attachments": [
            {"type": "ig_reel", "payload": {"url": "https://cdn/x.mp4", "title": "Cool reel"}}
        ]}}]}],
    }
    assert main._extract_reel_urls(payload) == [("https://cdn/x.mp4", "Cool reel")]


def test_extract_multiple_reels_and_title_default():
    payload = {"entry": [{"messaging": [
        {"message": {"attachments": [{"type": "ig_reel", "payload": {"url": "a"}}]}},
        {"message": {"attachments": [{"type": "reel", "payload": {"url": "b", "title": "B"}}]}},
    ]}]}
    assert main._extract_reel_urls(payload) == [("a", ""), ("b", "B")]


def test_extract_ignores_echoes_nonreels_and_missing_url():
    payload = {"entry": [{"messaging": [
        {"message": {"is_echo": True, "attachments": [{"type": "ig_reel", "payload": {"url": "echo"}}]}},
        {"message": {"attachments": [{"type": "image", "payload": {"url": "img.jpg"}}]}},
        {"message": {"attachments": [{"type": "ig_reel", "payload": {}}]}},  # no url
        {"read": {"mid": "x"}},  # a read receipt, not a message
    ]}]}
    assert main._extract_reel_urls(payload) == []


def test_extract_handles_empty_and_malformed():
    assert main._extract_reel_urls({}) == []
    assert main._extract_reel_urls({"entry": [{}]}) == []
    assert main._extract_reel_urls({"entry": [{"messaging": [{}]}]}) == []
    assert main._extract_reel_urls({"entry": None}) == []
