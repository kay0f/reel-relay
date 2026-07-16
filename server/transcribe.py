"""Transcription: caption (VTT) parsing, audio extraction, Groq whisper-large-v3."""
import logging
import re
import subprocess
from pathlib import Path

import httpx

from . import config

log = logging.getLogger("reel-relay.transcribe")

# 00:00:01.234 or 00:01.234 (hours optional); VTT uses '.', SRT uses ','
_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
_TAG_RE = re.compile(r"<[^>]*>")


def _ts_to_seconds(m: re.Match) -> float:
    hours = int(m.group(1) or 0)
    return round(hours * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000, 3)


def _strip_metadata_blocks(lines: list[str]) -> list[str]:
    """Drop NOTE/STYLE/REGION blocks (their content must not become cue text)."""
    out: list[str] = []
    skipping = False
    for line in lines:
        if skipping:
            if not line.strip():
                skipping = False
            continue
        if re.match(r"^(NOTE|STYLE|REGION)\b", line.strip()):
            skipping = True
            continue
        out.append(line)
    return out


def parse_vtt(text: str) -> list[dict]:
    """Parse WebVTT (incl. YouTube auto-caption style) into [{"start", "end", "text"}].

    Line-based rather than blank-line-block-based, because YouTube's rolling ASR
    format puts whitespace-only lines INSIDE cues and repeats the previous caption
    as the first payload line of each cue. Strips inline tags/word timestamps,
    skips header/NOTE/STYLE blocks and cue ids, deduplicates rolling repeats
    (extending the previous segment's end time instead of duplicating text).
    """
    lines = _strip_metadata_blocks(
        text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )
    timing_idx = [
        i for i, l in enumerate(lines) if "-->" in l and len(_TS_RE.findall(l)) >= 2
    ]

    segments: list[dict] = []
    prev_lines: list[str] = []
    for k, i in enumerate(timing_idx):
        matches = list(_TS_RE.finditer(lines[i]))
        start, end = _ts_to_seconds(matches[0]), _ts_to_seconds(matches[1])

        bound = timing_idx[k + 1] if k + 1 < len(timing_idx) else len(lines)
        payload = lines[i + 1 : bound]
        # the line directly before the next timing line is that cue's id (e.g. "2")
        # when it follows a blank line - don't treat it as this cue's text
        if k + 1 < len(timing_idx) and payload:
            j = bound - 1
            if lines[j].strip() and (j - 1 <= i or not lines[j - 1].strip()):
                payload = lines[i + 1 : j]

        cue_lines = [re.sub(r"\s+", " ", _TAG_RE.sub("", l)).strip() for l in payload]
        cue_lines = [l for l in cue_lines if l]
        new_lines = [l for l in cue_lines if l not in prev_lines]  # rolling repeats
        prev_lines = cue_lines

        body = " ".join(new_lines).strip()
        if not body:
            if cue_lines and segments:
                segments[-1]["end"] = max(segments[-1]["end"], end)  # pure repeat
            continue
        if segments and segments[-1]["text"] == body:
            segments[-1]["end"] = max(segments[-1]["end"], end)
            continue
        segments.append({"start": start, "end": end, "text": body})
    return segments


def parse_groq_verbose_json(data: dict) -> list[dict]:
    """Normalize a Groq/OpenAI verbose_json transcription response."""
    segments = data.get("segments")
    out: list[dict] = []
    if isinstance(segments, list):
        for seg in segments:
            try:
                text = str(seg.get("text", "")).strip()
                if not text:
                    continue
                out.append(
                    {
                        "start": round(float(seg["start"]), 2),
                        "end": round(float(seg["end"]), 2),
                        "text": text,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    if not out:
        text = str(data.get("text") or "").strip()
        if text:
            try:
                end = round(float(data.get("duration") or 0.0), 2)
            except (TypeError, ValueError):
                end = 0.0
            out = [{"start": 0.0, "end": end, "text": text}]
    return out


def extract_audio(video: Path, out_mp3: Path) -> Path:
    """Mono 16 kHz 64 kbps mp3 - small enough for Groq's upload limits."""
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
            "-y", str(out_mp3),
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not out_mp3.exists():
        tail = proc.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"audio extraction failed: {tail}")
    return out_mp3


def transcribe_with_groq(audio: Path, api_key: str) -> list[dict]:
    with open(audio, "rb") as f:
        resp = httpx.post(
            config.GROQ_TRANSCRIPTION_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (audio.name, f, "audio/mpeg")},
            data={
                "model": config.GROQ_WHISPER_MODEL,
                "response_format": "verbose_json",
                "temperature": "0",
            },
            timeout=httpx.Timeout(180.0, connect=15.0),
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Groq transcription failed ({resp.status_code}): {resp.text[:300]}")
    return parse_groq_verbose_json(resp.json())


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(max(seconds, 0)), 60)
    return f"{m}:{s:02d}"


def format_transcript(segments: list[dict]) -> str:
    if not segments:
        return "(no transcript available)"
    return "\n".join(f"[{_fmt_ts(s['start'])}-{_fmt_ts(s['end'])}] {s['text']}" for s in segments)
