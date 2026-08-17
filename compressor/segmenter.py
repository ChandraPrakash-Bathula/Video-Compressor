"""
Scene-segmented encoding.

A single CRF for a whole video is a compromise: the value that keeps a
high-motion shot at the quality floor wastes bits on the static shot next to
it. Encoding each scene at its own CRF removes that compromise -- every shot
lands on the floor rather than above it, and the bits saved on easy shots are
simply not spent.

Two costs have to be controlled for this to be worth it:

*Search cost.* A full binary search per segment would be N x 5 probe encodes.
Instead the global search runs once to establish a baseline CRF, and each
segment refines within a narrow band around it in at most three probes. Most of
the gain, a fraction of the work.

*Memory.* Segments encode in parallel, but x265 at slow presets holds roughly a
gigabyte per process at 1080p. Sizing the pool purely on core count is how a
machine ends up swapping, so the pool is sized on whichever of RAM or cores is
scarcer.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .encoders import CodecSpec, build_filter_args, build_video_args
from .environment import Environment, get_environment, run
from .probe import SourceInfo

_SCD_RE = re.compile(r"lavfi\.scd\.time:\s*([0-9.]+)")

# Segment length bounds, in seconds. Too short and per-segment overhead plus
# keyframe cost dominates; too long and parallelism starves.
MIN_SEGMENT = 4.0
MAX_SEGMENT = 30.0

# Rough working set per encoder process, by pixel count. Used to size the pool.
_BYTES_PER_PIXEL_WORKING_SET = 550


@dataclass
class Segment:
    index: int
    start: float
    end: float
    crf: Optional[int] = None
    vmaf: Optional[float] = None
    path: Optional[str] = None

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_scenes(
    source: SourceInfo,
    env: Optional[Environment] = None,
    threshold: float = 12.0,
) -> list[float]:
    """
    Return scene-cut timestamps in seconds.

    Uses the `scdet` filter, which reports a cut score per frame. A missed cut
    is harmless -- `build_segments` caps segment length regardless -- so a
    conservative threshold is preferred over one that fragments on motion.
    """
    env = env or get_environment()
    result = run([
        env.ffmpeg, "-hide_banner", "-nostats",
        "-i", source.path,
        "-vf", f"scdet=s=1:t={threshold}",
        "-f", "null", "-",
    ], timeout=3600)

    cuts = [float(match) for match in _SCD_RE.findall(result.stderr or "")]
    return sorted(t for t in cuts if 0 < t < source.duration)


def build_segments(
    duration: float,
    cuts: list[float],
    min_length: float = MIN_SEGMENT,
    max_length: float = MAX_SEGMENT,
) -> list[Segment]:
    """
    Turn cut points into encodable spans.

    Cuts closer together than `min_length` are merged; spans longer than
    `max_length` are split so parallelism has work to distribute.
    """
    boundaries = [0.0]
    for cut in cuts:
        if cut - boundaries[-1] >= min_length and duration - cut >= min_length:
            boundaries.append(cut)
    boundaries.append(duration)

    segments: list[Segment] = []
    for start, end in zip(boundaries, boundaries[1:]):
        span = end - start
        if span <= max_length:
            segments.append(Segment(len(segments), start, end))
            continue
        # Split evenly rather than leaving a short tail.
        pieces = int(span // max_length) + 1
        step = span / pieces
        for piece in range(pieces):
            segments.append(Segment(
                len(segments), start + piece * step,
                start + (piece + 1) * step if piece < pieces - 1 else end,
            ))
    return segments


@dataclass
class SegmentationPlan:
    """Whether segmenting this video is worth the CPU, decided before spending it."""

    segments: list[Segment]
    scene_bpp: list[float]
    variation: float                  # coefficient of variation of per-scene bpp
    cheap_byte_share: float           # share of bytes in below-median-bpp scenes
    worthwhile: bool
    reason: str

    def summary(self) -> dict:
        return {
            "scenes": len(self.segments),
            "variation": round(self.variation, 3),
            "cheap_byte_share": round(self.cheap_byte_share, 3),
            "worthwhile": self.worthwhile,
            "reason": self.reason,
        }


def scene_bitrates(
    source: SourceInfo,
    segments: list[Segment],
    env: Optional[Environment] = None,
) -> list[float]:
    """
    Bits per pixel per frame for each scene, read from the source's own packets.

    Packet sizes come from the container index, so this costs a metadata read
    rather than a decode -- cheap enough to run before deciding anything.
    """
    env = env or get_environment()
    result = run([
        env.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,size",
        "-of", "csv=p=0", source.path,
    ], timeout=1800)

    totals = [0.0] * len(segments)
    for line in (result.stdout or "").splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            timestamp, size = float(parts[0]), int(parts[1])
        except ValueError:
            continue
        for index, segment in enumerate(segments):
            if segment.start <= timestamp < segment.end:
                totals[index] += size
                break

    per_frame_pixels = max(source.pixels * source.fps, 1)
    return [
        (total * 8 / max(segment.duration, 0.001)) / per_frame_pixels
        for total, segment in zip(totals, segments)
    ]


def plan_segmentation(
    source: SourceInfo,
    env: Optional[Environment] = None,
    min_variation: float = 0.35,
    min_cheap_share: float = 0.15,
) -> SegmentationPlan:
    """
    Decide whether per-scene CRF can pay for itself, before encoding anything.

    Segmenting is not free: it costs a refinement probe per scene and it makes
    the quality floor *stricter* (every scene must clear it individually, where
    a global search only has to clear it on average). Two conditions have to
    hold for that to be repaid.

    *Scenes must actually differ.* Measured as the coefficient of variation of
    per-scene bpp. If every scene costs about the same, one CRF is already the
    right answer for all of them and segmenting buys nothing but encode time.

    *The cheap scenes must hold enough of the bytes to matter.* This is the
    condition that measurement showed to be the real one. Savings come from
    compressing easy scenes harder, so if the below-median scenes only hold a
    few percent of the file, squeezing them cannot move the total no matter how
    much their CRF rises.
    """
    env = env or get_environment()
    cuts = detect_scenes(source, env)
    segments = build_segments(source.duration, cuts)

    if len(segments) < 2:
        return SegmentationPlan(
            segments, [], 0.0, 0.0, False,
            "Only one scene was detected, so there is nothing to vary.",
        )

    bpp = scene_bitrates(source, segments, env)
    total = sum(bpp)
    if total <= 0:
        return SegmentationPlan(
            segments, bpp, 0.0, 0.0, False,
            "Per-scene bitrates could not be read from this container.",
        )

    mean = total / len(bpp)
    variance = sum((value - mean) ** 2 for value in bpp) / len(bpp)
    variation = (variance ** 0.5) / mean if mean else 0.0

    # Weight by duration: a scene's share of the file, not of the list.
    weighted = [value * segment.duration for value, segment in zip(bpp, segments)]
    weighted_total = sum(weighted) or 1.0
    median = sorted(bpp)[len(bpp) // 2]
    cheap_share = sum(
        weight for value, weight in zip(bpp, weighted) if value <= median
    ) / weighted_total

    if variation < min_variation:
        return SegmentationPlan(
            segments, bpp, variation, cheap_share, False,
            f"Scene complexity barely varies (variation {variation:.2f} < "
            f"{min_variation:.2f}); a single CRF is already right for every scene.",
        )
    if cheap_share < min_cheap_share:
        return SegmentationPlan(
            segments, bpp, variation, cheap_share, False,
            f"The easy scenes hold only {cheap_share * 100:.0f}% of the bytes, so "
            f"compressing them harder cannot meaningfully shrink the file.",
        )

    return SegmentationPlan(
        segments, bpp, variation, cheap_share, True,
        f"{len(segments)} scenes, complexity variation {variation:.2f}, "
        f"{cheap_share * 100:.0f}% of bytes in easy scenes.",
    )


def worker_count(source: SourceInfo, requested: Optional[int] = None) -> int:
    """
    Pick a parallel encode width from whichever of RAM or cores is scarcer.

    Encoding is not embarrassingly parallel in practice: each process holds
    reference frames and lookahead buffers proportional to frame size, so a
    core-count-sized pool on a memory-light machine swaps instead of scaling.
    """
    cores = os.cpu_count() or 4
    if requested:
        return max(1, min(requested, cores))

    per_process = source.pixels * _BYTES_PER_PIXEL_WORKING_SET
    try:
        if sys.platform == "darwin":
            total_ram = int(subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
            ).stdout.strip())
        elif sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            total_ram = int(status.ullTotalPhys)
        else:
            total_ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        total_ram = 8 * 1024 ** 3

    # Leave a quarter of RAM for the OS, page cache and the rest of the app.
    budget = total_ram * 0.75
    by_memory = max(1, int(budget // max(per_process, 1)))
    return max(1, min(cores, by_memory))


def _encode_segment(job: dict) -> dict:
    """
    Encode one span. Runs in a worker process, so arguments are plain data.
    """
    cmd = [
        job["ffmpeg"], "-y", "-v", "error",
        "-ss", f"{job['start']:.3f}",
        "-to", f"{job['end']:.3f}",
        "-i", job["input"],
        "-map", "0:v:0", "-an",
    ]
    cmd += job["video_args"]
    cmd += job["filter_args"]
    # Deliberately no `-g`: the first frame of any encode is already an IDR,
    # which is all stream-copy concatenation needs. Forcing a short GOP on top
    # of that just multiplies I-frames, and their cost easily exceeds every bit
    # the per-scene CRF tuning saves.
    cmd += [job["output"]]

    completed = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return {
        "index": job["index"],
        "ok": completed.returncode == 0 and os.path.exists(job["output"]),
        "output": job["output"],
        "error": (completed.stderr or "")[-400:],
    }


def encode_segments(
    source: SourceInfo,
    segments: list[Segment],
    spec: CodecSpec,
    options,
    env: Environment,
    workdir: Path,
    progress: Optional[Callable[[float, str], None]] = None,
    workers: Optional[int] = None,
) -> list[Segment]:
    """Encode every segment at its own CRF, in parallel. Returns them in order."""
    pool_size = worker_count(source, workers)
    jobs = []
    for segment in segments:
        output = workdir / f"seg_{segment.index:05d}.mp4"
        segment.path = str(output)
        jobs.append({
            "index": segment.index,
            "ffmpeg": env.ffmpeg,
            "input": source.path,
            "output": str(output),
            "start": segment.start,
            "end": segment.end,
            "video_args": build_video_args(
                spec, segment.crf or spec.default_crf, options.speed, source, options.ten_bit
            ),
            "filter_args": build_filter_args(source, options.max_height),
        })

    done = 0
    failures: list[str] = []
    with ProcessPoolExecutor(max_workers=pool_size) as pool:
        futures = {pool.submit(_encode_segment, job): job for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            done += 1
            if not result["ok"]:
                failures.append(f"segment {result['index']}: {result['error']}")
            if progress:
                progress(done / len(jobs) * 100,
                         f"Encoded {done}/{len(jobs)} scenes on {pool_size} workers")

    if failures:
        raise RuntimeError("Segment encoding failed:\n" + "\n".join(failures[:3]))
    return segments


def concat_segments(
    segments: list[Segment],
    source: SourceInfo,
    output_path: str,
    audio_args: list[str],
    env: Environment,
    workdir: Path,
) -> None:
    """
    Join encoded segments and mux the audio.

    Video is stream-copied: the segments already share encoder settings and
    each opens on a keyframe, so re-encoding here would add a generation of
    loss for nothing. Audio is taken from the original in one pass rather than
    per segment, which avoids drift and gaps at the joins.
    """
    listing = workdir / "segments.txt"
    listing.write_text("".join(
        f"file '{Path(segment.path).as_posix()}'\n"
        for segment in sorted(segments, key=lambda s: s.index)
    ))

    cmd = [
        env.ffmpeg, "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
    ]
    has_audio = source.audio is not None and audio_args != ["-an"]
    if has_audio:
        cmd += ["-i", source.path]

    cmd += ["-map", "0:v:0", "-c:v", "copy"]
    if has_audio:
        cmd += ["-map", "1:a:0"] + audio_args
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", output_path]

    result = run(cmd, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(
            "Joining the encoded scenes failed.\n" + (result.stderr or "")[-400:]
        )
