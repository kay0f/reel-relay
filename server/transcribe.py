"""Transcription: caption (VTT) parsing, audio extraction, OpenAI-compatible Whisper (NanoGPT/Groq)."""
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


def parse_groq_verbose_json(data: dict, fallback_duration: float = 0.0) -> list[dict]:
    """Normalize an OpenAI-compatible (Groq/NanoGPT/OpenAI) transcription response.

    Accepts verbose_json (segments with timestamps) or plain json ({"text": ...});
    NanoGPT's native shape ({"transcription": ...}) is accepted too.
    """
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
        text = str(data.get("text") or data.get("transcription") or "").strip()
        if text:
            try:
                end = round(float(data.get("duration") or fallback_duration or 0.0), 2)
            except (TypeError, ValueError):
                end = 0.0
            out = [{"start": 0.0, "end": end, "text": text}]
    return out


def extract_audio(video: Path, out_mp3: Path, bitrate: str = "64k") -> Path:
    """Mono 16 kHz mp3 - small enough for typical STT upload limits (NanoGPT: ~3 MB)."""
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", bitrate,
            "-y", str(out_mp3),
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not out_mp3.exists():
        tail = proc.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"audio extraction failed: {tail}")
    return out_mp3


def extract_audio_for_upload(video: Path, workdir: Path) -> Path:
    """Extract audio, re-encoding at a lower bitrate if it exceeds the upload limit."""
    audio = extract_audio(video, workdir / "audio.mp3")
    for bitrate in ("32k", "16k"):
        if audio.stat().st_size <= config.STT_MAX_UPLOAD_BYTES:
            break
        audio = extract_audio(video, workdir / f"audio_{bitrate}.mp3", bitrate=bitrate)
    if audio.stat().st_size > config.STT_MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"audio is {audio.stat().st_size} bytes even at 16 kbps; over the "
            f"{config.STT_MAX_UPLOAD_BYTES}-byte upload limit"
        )
    return audio


def _post_transcription(audio: Path, api_key: str, response_format: str) -> httpx.Response:
    data = {"model": config.STT_MODEL, "response_format": response_format, "temperature": "0"}
    if config.STT_LANGUAGE:
        data["language"] = config.STT_LANGUAGE
    with open(audio, "rb") as f:
        return httpx.post(
            config.STT_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (audio.name, f, "audio/mpeg")},
            data=data,
            timeout=httpx.Timeout(180.0, connect=15.0),
        )


def transcribe_audio(audio: Path, api_key: str, duration: float = 0.0) -> list[dict]:
    """Transcribe via the configured OpenAI-compatible endpoint.

    Asks for verbose_json (timestamped segments); if the provider rejects that format,
    retries with plain json and returns one segment spanning the whole clip.
    """
    resp = _post_transcription(audio, api_key, "verbose_json")
    if resp.status_code in (400, 415, 422):
        log.info("verbose_json rejected (%s); retrying with json", resp.status_code)
        resp = _post_transcription(audio, api_key, "json")
    if resp.status_code != 200:
        raise RuntimeError(f"transcription failed ({resp.status_code}): {resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError:
        data = {"text": resp.text}
    if not isinstance(data, dict):
        data = {"text": str(data)}
    return parse_groq_verbose_json(data, fallback_duration=duration)


def transcribe_with_groq(audio: Path, api_key: str) -> list[dict]:
    """Backward-compatible alias (the endpoint is now configurable via STT_*)."""
    return transcribe_audio(audio, api_key)


def stt_source_label() -> str:
    host = httpx.URL(config.STT_API_URL).host or "stt"
    return f"{host.removeprefix('api.')}/{config.STT_MODEL}"


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(max(seconds, 0)), 60)
    return f"{m}:{s:02d}"


def format_transcript(segments: list[dict]) -> str:
    if not segments:
        return "(no transcript available)"
    return "\n".join(f"[{_fmt_ts(s['start'])}-{_fmt_ts(s['end'])}] {s['text']}" for s in segments)
