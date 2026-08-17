"""
Source analysis.

The size reduction available from a video is not a property of the encoder --
it is a property of how wastefully the source was encoded in the first place.
This module measures that waste so the engine can predict, before spending any
CPU, roughly how much it will be able to save.

The key metric is bits per pixel per frame (BPP):

    bpp = video_bitrate / (width * height * fps)

A 1080p30 phone recording typically lands around 0.10-0.20 BPP. A well-encoded
H.265 download sits near 0.02. The first has enormous headroom; the second has
almost none, no matter which encoder you point at it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

from .environment import Environment, get_environment, run


class ProbeError(RuntimeError):
    """Raised when a file cannot be read as video."""


# Codec generation multipliers. A source already in a modern codec has less
# slack left, so temper the prediction rather than promising the impossible.
_CODEC_HEADROOM = {
    "av1": 0.35,
    "hevc": 0.55,
    "h265": 0.55,
    "vp9": 0.60,
    "h264": 1.00,
    "avc": 1.00,
    "vp8": 1.10,
    "mpeg4": 1.25,
    "mpeg2video": 1.35,
    "prores": 1.50,
    "dnxhd": 1.50,
    "rawvideo": 1.60,
}

# (min_bpp, label, baseline reduction range) for an H.264-class source.
_WASTE_TIERS = [
    (0.200, "extremely wasteful", (88, 95)),
    (0.100, "very wasteful", (80, 92)),
    (0.050, "typical", (65, 82)),
    (0.025, "fairly efficient", (45, 65)),
    (0.000, "already well compressed", (10, 40)),
]


@dataclass
class AudioInfo:
    codec: str
    bitrate: int          # bits per second, 0 if unknown
    channels: int
    sample_rate: int


@dataclass
class SourceInfo:
    """Everything the engine needs to know about an input file."""

    path: str
    size_bytes: int
    duration: float
    container: str

    width: int
    height: int
    fps: float
    video_codec: str
    pix_fmt: str
    bit_depth: int
    video_bitrate: int          # bits per second
    audio: Optional[AudioInfo]

    bpp: float
    waste_label: str
    predicted_reduction: tuple[int, int]

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def summary(self) -> dict:
        """JSON-safe view for the API and UI."""
        low, high = self.predicted_reduction
        return {
            "filename": Path(self.path).name,
            "size_mb": round(self.size_mb, 2),
            "duration": round(self.duration, 1),
            "resolution": self.resolution,
            "fps": round(self.fps, 2),
            "video_codec": self.video_codec,
            "bit_depth": self.bit_depth,
            "video_bitrate_kbps": round(self.video_bitrate / 1000),
            "audio_codec": self.audio.codec if self.audio else None,
            "audio_bitrate_kbps": round(self.audio.bitrate / 1000) if self.audio else 0,
            "bpp": round(self.bpp, 4),
            "waste_label": self.waste_label,
            "predicted_reduction": [low, high],
        }


def _parse_fps(stream: dict) -> float:
    """Prefer avg_frame_rate; fall back to r_frame_rate. Both are 'num/den'."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key, "0/0")
        try:
            value = float(Fraction(raw))
        except (ZeroDivisionError, ValueError):
            continue
        if 0 < value < 1000:
            return value
    return 30.0


def _bit_depth(stream: dict) -> int:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            depth = int(stream.get(key, 0))
        except (TypeError, ValueError):
            continue
        if depth > 0:
            return depth
    # Infer from the pixel format name, e.g. yuv420p10le.
    pix_fmt = stream.get("pix_fmt", "")
    for depth in (16, 14, 12, 10):
        if f"p{depth}" in pix_fmt:
            return depth
    return 8


def _classify(bpp: float, codec: str) -> tuple[str, tuple[int, int]]:
    """Turn BPP plus source codec into a human label and a reduction estimate."""
    label, (low, high) = "typical", (65, 82)
    for threshold, tier_label, tier_range in _WASTE_TIERS:
        if bpp >= threshold:
            label, (low, high) = tier_label, tier_range
            break

    headroom = _CODEC_HEADROOM.get(codec.lower(), 1.0)
    if headroom != 1.0:
        low = int(low * headroom)
        high = int(high * headroom)
        if headroom < 1.0:
            label = f"{label} (already {codec.upper()})"

    return label, (max(0, min(low, 95)), max(0, min(high, 96)))


def probe(path: str, env: Optional[Environment] = None) -> SourceInfo:
    """Analyse a video file. Raises ProbeError if it is not usable video."""
    env = env or get_environment()
    file_path = Path(path)

    if not file_path.exists():
        raise ProbeError(f"File not found: {path}")

    size_bytes = file_path.stat().st_size
    if size_bytes == 0:
        raise ProbeError("File is empty.")

    result = run([
        env.ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(file_path),
    ])
    if result.returncode != 0 or not result.stdout.strip():
        raise ProbeError("Not a readable media file, or the format is unsupported.")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"Could not parse media metadata: {exc}") from exc

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    if video is None:
        raise ProbeError("No video stream found — this looks like audio or an image.")

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ProbeError("Could not determine video dimensions.")

    duration = float(fmt.get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ProbeError("Could not determine video duration.")

    fps = _parse_fps(video)

    audio: Optional[AudioInfo] = None
    if audio_stream is not None:
        audio = AudioInfo(
            codec=audio_stream.get("codec_name", "unknown"),
            bitrate=int(float(audio_stream.get("bit_rate") or 0)),
            channels=int(audio_stream.get("channels") or 2),
            sample_rate=int(float(audio_stream.get("sample_rate") or 48000)),
        )
        if audio.bitrate == 0:
            # Not tagged (common in MKV). Assume a typical stereo rate so the
            # video bitrate derivation below stays sane.
            audio.bitrate = 128_000 * max(1, audio.channels // 2)

    # Prefer the tagged video bitrate; otherwise derive it from the file size.
    video_bitrate = int(float(video.get("bit_rate") or 0))
    if video_bitrate <= 0:
        total_bps = size_bytes * 8 / duration
        video_bitrate = int(max(total_bps - (audio.bitrate if audio else 0), 1000))

    codec = video.get("codec_name", "unknown")
    bpp = video_bitrate / max(width * height * fps, 1)
    waste_label, predicted = _classify(bpp, codec)

    return SourceInfo(
        path=str(file_path),
        size_bytes=size_bytes,
        duration=duration,
        container=fmt.get("format_name", "unknown"),
        width=width,
        height=height,
        fps=fps,
        video_codec=codec,
        pix_fmt=video.get("pix_fmt", "yuv420p"),
        bit_depth=_bit_depth(video),
        video_bitrate=video_bitrate,
        audio=audio,
        bpp=bpp,
        waste_label=waste_label,
        predicted_reduction=predicted,
    )
