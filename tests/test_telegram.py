"""Tests for Telegram update parsing (URL extraction from shared reels)."""
from server import telegram


def test_extract_url_from_text():
    upd = {"message": {"text": "https://www.instagram.com/reel/ABC123/", "chat": {"id": 1}}}
    assert telegram.extract_urls(upd) == ["https://www.instagram.com/reel/ABC123/"]


def test_extract_url_from_caption():
    upd = {"message": {"caption": "check this https://www.instagram.com/reel/XYZ/ out"}}
    assert telegram.extract_urls(upd) == ["https://www.instagram.com/reel/XYZ/"]


def test_extract_url_from_text_link_entity():
    upd = {"message": {"text": "cool reel", "entities": [
        {"type": "text_link", "url": "https://www.instagram.com/reel/E1/"}
    ]}}
    assert telegram.extract_urls(upd) == ["https://www.instagram.com/reel/E1/"]


def test_extract_strips_trailing_punctuation_and_dedupes():
    upd = {"message": {"text": "https://insta.gr/a). https://insta.gr/a)"}}
    assert telegram.extract_urls(upd) == ["https://insta.gr/a"]


def test_extract_none_when_no_url():
    assert telegram.extract_urls({"message": {"text": "hello there"}}) == []
    assert telegram.extract_urls({"message": {"text": "/start"}}) == []
    assert telegram.extract_urls({}) == []


def test_extract_handles_edited_and_channel_updates():
    assert telegram.extract_urls({"edited_message": {"text": "https://x.co/1"}}) == ["https://x.co/1"]
    assert telegram.extract_urls({"channel_post": {"text": "https://x.co/2"}}) == ["https://x.co/2"]


def test_message_of():
    assert telegram.message_of({"message": {"text": "a"}}) == {"text": "a"}
    assert telegram.message_of({}) == {}
