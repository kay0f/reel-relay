"""Unit tests for VTT + Groq transcript parsing, and offline audio extraction."""
import json
import subprocess

import pytest

from conftest import requires_ffmpeg
from server import transcribe

SAMPLE_VTT = """\
WEBVTT
Kind: captions
Language: en

NOTE
this block should be ignored

STYLE
::cue { color: lime }

1
00:00:00.000 --> 00:00:02.500
Hello <b>world</b>

2
00:00:02.500 --> 00:00:05.000 align:start position:0%
Second line
continues here
"""


def test_parse_vtt_basic():
    segs = transcribe.parse_vtt(SAMPLE_VTT)
    assert segs == [
        {"start": 0.0, "end": 2.5, "text": "Hello world"},
        {"start": 2.5, "end": 5.0, "text": "Second line continues here"},
    ]


def test_parse_vtt_no_hours_and_comma_millis():
    vtt = "WEBVTT\n\n00:01.250 --> 00:03,750\nshort form\n"
    segs = transcribe.parse_vtt(vtt)
    assert segs == [{"start": 1.25, "end": 3.75, "text": "short form"}]


def test_parse_vtt_with_hours():
    vtt = "WEBVTT\n\n01:02:03.000 --> 01:02:04.500\nlate cue\n"
    segs = transcribe.parse_vtt(vtt)
    assert segs[0]["start"] == pytest.approx(3723.0)
    assert segs[0]["end"] == pytest.approx(3724.5)


def test_parse_vtt_merges_rolling_duplicates():
    vtt = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\nsame text\n\n"
        "00:00:02.000 --> 00:00:04.000\nsame text\n\n"
        "00:00:04.000 --> 00:00:06.000\nnew text\n"
    )
    segs = transcribe.parse_vtt(vtt)
    assert segs == [
        {"start": 0.0, "end": 4.0, "text": "same text"},
        {"start": 4.0, "end": 6.0, "text": "new text"},
    ]


def test_parse_vtt_skips_empty_cues_and_inline_timestamp_tags():
    vtt = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.000\n<00:00:00.500><c>styled</c> words\n\n"
        "00:00:01.000 --> 00:00:02.000\n   \n"
    )
    segs = transcribe.parse_vtt(vtt)
    assert segs == [{"start": 0.0, "end": 1.0, "text": "styled words"}]


def test_parse_vtt_garbage_returns_empty():
    assert transcribe.parse_vtt("") == []
    assert transcribe.parse_vtt("WEBVTT\n\nnot a cue at all\n") == []


# Real YouTube ASR rolling format: whitespace-only lines INSIDE cues, the previous
# caption repeated as the first payload line of each cue, ~10ms bridge cues, and
# per-word inline timestamps.
YT_ASR_VTT = (
    "WEBVTT\n"
    "Kind: captions\n"
    "Language: en\n"
    "\n"
    "00:00:00.000 --> 00:00:01.919 align:start position:0%\n"
    " \n"
    "We're<00:00:00.960><c> no</c><c> strangers</c><c> to</c>\n"
    "\n"
    "00:00:01.919 --> 00:00:01.929 align:start position:0%\n"
    "We're no strangers to\n"
    " \n"
    "\n"
    "00:00:01.929 --> 00:00:03.500 align:start position:0%\n"
    "We're no strangers to\n"
    "love<00:00:02.500><c> you</c><c> know</c>\n"
)


def test_parse_vtt_youtube_rolling_asr():
    segs = transcribe.parse_vtt(YT_ASR_VTT)
    assert segs == [
        {"start": 0.0, "end": 1.929, "text": "We're no strangers to"},
        {"start": 1.929, "end": 3.5, "text": "love you know"},
    ], "rolling repeats must be deduplicated, not duplicated with skewed timestamps"


def test_parse_groq_verbose_json_segments():
    data = {
        "text": "Hello there. General Kenobi.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 1.2, "text": " Hello there. "},
            {"id": 1, "start": 1.2, "end": 3.0, "text": "General Kenobi."},
            {"id": 2, "start": 3.0, "end": 3.5, "text": "   "},
        ],
    }
    segs = transcribe.parse_groq_verbose_json(data)
    assert segs == [
        {"start": 0.0, "end": 1.2, "text": "Hello there."},
        {"start": 1.2, "end": 3.0, "text": "General Kenobi."},
    ]


def test_parse_groq_verbose_json_falls_back_to_text():
    data = {"text": "just a blob", "duration": 12.34}
    assert transcribe.parse_groq_verbose_json(data) == [
        {"start": 0.0, "end": 12.34, "text": "just a blob"}
    ]


def test_parse_groq_verbose_json_skips_malformed_segments():
    data = {"segments": [{"start": "x", "end": 1, "text": "bad"}, {"start": 0, "end": 1, "text": "ok"}]}
    assert transcribe.parse_groq_verbose_json(data) == [{"start": 0.0, "end": 1.0, "text": "ok"}]


def test_parse_groq_verbose_json_empty():
    assert transcribe.parse_groq_verbose_json({}) == []
    assert transcribe.parse_groq_verbose_json({"text": "  "}) == []


def test_format_transcript():
    assert transcribe.format_transcript([]) == "(no transcript available)"
    out = transcribe.format_transcript(
        [{"start": 0.0, "end": 4.2, "text": "hi"}, {"start": 65.0, "end": 70.0, "text": "later"}]
    )
    assert out == "[0:00–0:04] hi\n[1:05–1:10] later"


@requires_ffmpeg
def test_extract_audio_mono_16k(synth_clip, tmp_path):
    mp3 = transcribe.extract_audio(synth_clip, tmp_path / "audio.mp3")
    assert mp3.stat().st_size > 0
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=channels,sample_rate,codec_name",
            "-of", "json", str(mp3),
        ],
        capture_output=True, check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["channels"] == 1
    assert stream["sample_rate"] == "16000"
    assert stream["codec_name"] == "mp3"
