"""
VMAF-guided CRF search.

This is what separates "compressed a lot" from "compressed a lot without
visible quality loss". Rather than guessing a CRF from a lookup table, the
engine encodes short samples taken from across the video at several candidate
CRFs, scores each against the untouched source with Netflix's VMAF metric, and
binary-searches for the *highest* CRF -- the smallest file -- that still meets
the quality bar.

VMAF is a perceptual metric trained on human ratings:

    >= 95   differences are not perceptible in normal viewing
    93-95   perceptible only in a side-by-side freeze-frame
    90-93   very good; occasional soft frame under scrutiny
    < 88    visible softening

Sampling matters as much as the metric. Samples are spread across the runtime
and skip the opening and closing few percent, which are usually black frames,
titles or credits -- all trivially compressible and wildly unrepresentative.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .encoders import CodecSpec, build_video_args
from .environment import Environment, run
from .probe import SourceInfo

ProgressFn = Callable[[str, float], None]

_VMAF_RE = re.compile(r"VMAF score:\s*([0-9.]+)")

# Samples per speed tier, and seconds per sample.
_SAMPLE_PLAN = {
    "quality": (5, 2.0),
    "balanced": (3, 2.0),
    "fast": (2, 1.5),
}


@dataclass
class SearchResult:
    """Outcome of the CRF search."""

    crf: int
    vmaf: float
    met_target: bool
    probes: list[tuple[int, float]]     # (crf, vmaf) in the order tried
    estimated_ratio: Optional[float]    # predicted output/source size ratio
    method: str                         # "vmaf" or "heuristic"
    # False when re-encoding this source cannot help -- either no CRF reaches
    # the quality target, or the one that does would not make a smaller file.
    viable: bool = True
    reason: str = ""

    def summary(self) -> dict:
        return {
            "crf": self.crf,
            "vmaf": round(self.vmaf, 2) if self.vmaf else None,
            "met_target": self.met_target,
            "probes": [{"crf": c, "vmaf": round(v, 2)} for c, v in self.probes],
            "method": self.method,
            "viable": self.viable,
            "estimated_ratio": round(self.estimated_ratio, 3) if self.estimated_ratio else None,
        }


class NoHeadroom(RuntimeError):
    """
    Raised when re-encoding would not produce a smaller file.

    Re-encoding an already-efficient video is strictly worse than leaving it
    alone: the result is bigger *and* a generation lossier. Refusing is the
    correct outcome, not a failure to try hard enough.
    """


def _sample_windows(duration: float, count: int, length: float) -> list[tuple[float, float]]:
    """Evenly spaced sample start times, avoiding the head and tail."""
    usable_start = duration * 0.05
    usable_end = max(duration * 0.95 - length, usable_start)
    span = usable_end - usable_start

    if span <= 0 or count <= 1:
        return [(max(0.0, (duration - length) / 2), min(length, duration))]

    step = span / (count - 1)
    return [(usable_start + i * step, length) for i in range(count)]


def _build_reference(
    source: SourceInfo,
    env: Environment,
    workdir: Path,
    count: int,
    length: float,
) -> Optional[Path]:
    """
    Extract sample windows into one lossless clip.

    Lossless matters: VMAF measures the encode against this file, so any loss
    here would be baked into the reference and every score would be inflated.
    Built once and reused across all probes, so the (possibly huge) source is
    decoded only a single time.
    """
    windows = _sample_windows(source.duration, count, length)
    reference = workdir / "reference.mkv"

    cmd = [env.ffmpeg, "-y", "-v", "error"]
    for start, dur in windows:
        # -ss before -i seeks by keyframe index rather than decoding forward.
        cmd += ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", source.path]

    if len(windows) > 1:
        streams = "".join(f"[{i}:v:0]" for i in range(len(windows)))
        cmd += ["-filter_complex", f"{streams}concat=n={len(windows)}:v=1:a=0[out]",
                "-map", "[out]"]
    else:
        cmd += ["-map", "0:v:0"]

    cmd += ["-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-an", str(reference)]

    result = run(cmd, timeout=1800)
    if result.returncode != 0 or not reference.exists() or reference.stat().st_size == 0:
        return None
    return reference


def _score(
    encoded: Path,
    reference: Path,
    env: Environment,
    subsample: int = 1,
) -> Optional[float]:
    """
    Run libvmaf over an encoded sample against its lossless reference.

    `settb=AVTB,setpts=N/FRAME_RATE/TB` on both inputs is load-bearing, not
    boilerplate. The reference is MKV (timebase 1/1000) and the probe is MP4
    (1/15360); libvmaf's framesync pairs frames by presentation timestamp, and
    that rounding drift silently mis-pairs one frame in three. The symptom is a
    score that sits near 75 and barely responds to CRF, because a third of the
    frames are being compared against their neighbours. Regenerating PTS from
    the frame index forces both streams onto identical constant-rate timing.
    """
    threads = max(1, (os.cpu_count() or 4))
    # Scoring every other frame halves the metric cost and moves the mean by
    # hundredths -- immaterial for choosing a CRF, so the search uses it while
    # any final verification does not.
    subsample_arg = f":n_subsample={subsample}" if subsample > 1 else ""
    result = run([
        env.ffmpeg, "-hide_banner", "-nostats",
        "-i", str(encoded), "-i", str(reference),
        "-lavfi",
        f"[0:v]settb=AVTB,setpts=N/FRAME_RATE/TB[dist];"
        f"[1:v]settb=AVTB,setpts=N/FRAME_RATE/TB[ref];"
        f"[dist][ref]libvmaf=n_threads={threads}:shortest=1"
        f"{subsample_arg}",
        "-f", "null", "-",
    ], timeout=1800)

    match = _VMAF_RE.search(result.stderr or "")
    return float(match.group(1)) if match else None


def _encode_probe(
    reference: Path,
    out_path: Path,
    crf: int,
    spec: CodecSpec,
    tier: str,
    source: SourceInfo,
    ten_bit: bool,
    env: Environment,
) -> bool:
    """Encode the sample clip at one candidate CRF, matching final settings."""
    cmd = [env.ffmpeg, "-y", "-v", "error", "-i", str(reference)]
    cmd += build_video_args(spec, crf, tier, source, ten_bit, probing=True)
    cmd += ["-an", str(out_path)]
    result = run(cmd, timeout=3600)
    return result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def _build_window_reference(
    source: SourceInfo,
    env: Environment,
    workdir: Path,
    start: float,
    duration: float,
) -> Optional[Path]:
    """Lossless extract of one time window, used as a per-segment reference."""
    reference = workdir / f"ref_{start:.2f}.mkv"
    result = run([
        env.ffmpeg, "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", source.path,
        "-map", "0:v:0", "-an",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast",
        str(reference),
    ], timeout=1800)
    if result.returncode != 0 or not reference.exists() or reference.stat().st_size == 0:
        return None
    return reference


def refine_segment_crf(
    source: SourceInfo,
    spec: CodecSpec,
    env: Environment,
    start: float,
    duration: float,
    baseline_crf: int,
    target_vmaf: float,
    tier: str = "quality",
    ten_bit: bool = False,
    band: int = 8,
) -> tuple[int, float]:
    """
    Find this scene's own CRF, seeded from the whole-video baseline.

    A full binary search per scene would cost five probes each; seeding from the
    baseline needs at most three, because the answer is nearly always within a
    few CRF of it. Scenes that scored comfortably above the floor get pushed
    higher (they were being given bits they did not need); scenes that fell
    short get pulled down.

    Returns the baseline unchanged if sampling fails, so a difficult scene
    degrades to current behaviour rather than breaking the encode.
    """
    # Sample at most 3s from the middle of the scene -- enough to characterise
    # it, short enough that three probes stay cheap.
    sample_length = min(3.0, duration)
    sample_start = start + max(0.0, (duration - sample_length) / 2)

    workdir = Path(tempfile.mkdtemp(prefix="vc_seg_"))
    try:
        reference = _build_window_reference(source, env, workdir, sample_start, sample_length)
        if reference is None:
            return baseline_crf, 0.0

        # Refinement may climb well past the global ceiling: an easy scene
        # tolerates far more compression than the video as a whole.
        low, high = spec.crf_range[0], spec.segment_crf_max
        best_crf, best_vmaf = None, 0.0
        tried: set[int] = set()

        def probe(crf: int) -> Optional[float]:
            crf = max(low, min(high, crf))
            if crf in tried:
                return None
            tried.add(crf)
            path = workdir / f"p_{crf}.mp4"
            if not _encode_probe(reference, path, crf, spec, tier, source, ten_bit, env):
                return None
            score = _score(path, reference, env, subsample=2)
            path.unlink(missing_ok=True)
            return score

        score = probe(baseline_crf)
        if score is None:
            return baseline_crf, 0.0
        if score >= target_vmaf:
            best_crf, best_vmaf = baseline_crf, score

        if score >= target_vmaf + 1.5:
            # Comfortably clear, so climb. Steps double because the scenes that
            # clear by a wide margin are usually the trivial ones -- a flat or
            # static shot can be tens of CRF above the baseline and still be
            # perceptually perfect, and a fixed small step never gets near it.
            candidate = baseline_crf
            step = max(2, band // 2)
            for _ in range(4):
                candidate = min(high, candidate + step)
                higher = probe(candidate)
                if higher is None or higher < target_vmaf:
                    break
                best_crf, best_vmaf = candidate, higher
                if candidate >= high:
                    break
                step *= 2
        elif score < target_vmaf:
            # Fell short: back off until it clears.
            for step in (band // 2, band):
                candidate = baseline_crf - step
                lower = probe(candidate)
                if lower is None:
                    break
                if lower >= target_vmaf:
                    best_crf, best_vmaf = max(low, candidate), lower
                    break

        if best_crf is None:
            # Nothing cleared the floor; the most conservative tried value is
            # the safest choice for this scene.
            return max(low, baseline_crf - band), best_vmaf
        return best_crf, best_vmaf

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def heuristic_crf(spec: CodecSpec, source: SourceInfo) -> SearchResult:
    """
    Fallback when VMAF is unavailable: pick a CRF from measured source waste.

    Less precise than the search but still source-aware, which already beats a
    fixed table. Wasteful sources tolerate a higher CRF because their excess
    bitrate was never carrying visual information to begin with.
    """
    low, high = spec.crf_range
    if source.bpp >= 0.20:
        position = 0.85
    elif source.bpp >= 0.10:
        position = 0.70
    elif source.bpp >= 0.05:
        position = 0.50
    elif source.bpp >= 0.025:
        position = 0.30
    else:
        position = 0.10

    crf = int(round(low + (high - low) * position))
    return SearchResult(
        crf=crf, vmaf=0.0, met_target=False, probes=[],
        estimated_ratio=None, method="heuristic",
    )


def find_optimal_crf(
    source: SourceInfo,
    spec: CodecSpec,
    env: Environment,
    target_vmaf: float = 95.0,
    tier: str = "quality",
    ten_bit: bool = False,
    progress: Optional[ProgressFn] = None,
) -> SearchResult:
    """
    Binary-search for the highest CRF whose VMAF still clears `target_vmaf`.

    Falls back to `heuristic_crf` if VMAF is missing or sample extraction
    fails, so compression always proceeds.
    """
    if not env.has_vmaf:
        return heuristic_crf(spec, source)

    count, length = _SAMPLE_PLAN.get(tier, _SAMPLE_PLAN["balanced"])
    # Very short clips cannot supply several distinct windows.
    if source.duration < length * 2:
        count, length = 1, max(0.5, min(length, source.duration * 0.8))

    workdir = Path(tempfile.mkdtemp(prefix="vc_probe_"))
    try:
        if progress:
            progress("Extracting quality samples", 0.0)

        reference = _build_reference(source, env, workdir, count, length)
        if reference is None:
            return heuristic_crf(spec, source)

        # Total seconds of real content in the reference, used to turn a probe's
        # byte size into a bitrate that is comparable with the source's.
        sample_seconds = max(count * length, 0.1)
        low, high = spec.crf_range
        probes: list[tuple[int, float]] = []
        best_crf: Optional[int] = None
        best_vmaf = 0.0
        best_ratio: Optional[float] = None

        # log2(range) iterations converge on the boundary CRF.
        max_iterations = 6
        iteration = 0

        while low <= high and iteration < max_iterations:
            iteration += 1
            mid = (low + high) // 2

            if progress:
                progress(f"Testing quality at CRF {mid}", iteration / max_iterations * 100)

            probe_file = workdir / f"probe_{mid}.mp4"
            if not _encode_probe(reference, probe_file, mid, spec, tier, source, ten_bit, env):
                break

            score = _score(probe_file, reference, env, subsample=2)
            if score is None:
                break

            # Predict the full output against the SOURCE, not against the
            # lossless reference -- the reference is many times larger than the
            # original, so comparing to it produces a meaningless ratio.
            probe_bps = probe_file.stat().st_size * 8 / sample_seconds
            ratio = probe_bps / max(source.video_bitrate, 1)

            probes.append((mid, score))
            probe_file.unlink(missing_ok=True)

            # Early bail-out. If this CRF already needs more bits than the
            # source *and* still misses the target, every remaining candidate is
            # worse on both counts: the search only moves to lower CRFs from
            # here, which means higher quality and bigger files. Stop now rather
            # than spending four more probe encodes to reach the same verdict.
            if score < target_vmaf and ratio > 1.0:
                return SearchResult(
                    crf=mid, vmaf=score, met_target=False,
                    probes=probes, estimated_ratio=ratio, method="vmaf",
                    viable=False,
                    reason=(
                        f"At CRF {mid} the result already needs {ratio * 100:.0f}% of "
                        f"the original's bitrate and still only reaches VMAF "
                        f"{score:.1f}. This source is encoded too efficiently for "
                        f"re-encoding to help."
                    ),
                )

            if score >= target_vmaf:
                # Quality bar met -- try to push the CRF higher for a smaller file.
                best_crf, best_vmaf, best_ratio = mid, score, ratio
                low = mid + 1
            else:
                high = mid - 1

        if best_crf is None:
            # Nothing reached the target. Falling back to the lowest CRF in
            # range would be the worst possible answer: it is the *highest*
            # quality setting, so it produces the biggest file -- bigger than
            # the source, after a full-length encode. Refuse instead.
            best_attempt = max(probes, key=lambda p: p[1]) if probes else (0, 0.0)
            return SearchResult(
                crf=best_attempt[0], vmaf=best_attempt[1], met_target=False,
                probes=probes, estimated_ratio=None, method="vmaf",
                viable=False,
                reason=(
                    f"No setting reached VMAF {target_vmaf:.0f} — the best was "
                    f"{best_attempt[1]:.1f} at CRF {best_attempt[0]}. This source is "
                    f"already encoded efficiently enough that matching it that "
                    f"closely costs more bits than it already uses."
                ),
            )

        # Met the quality bar, but check it is actually worth doing. Samples are
        # drawn from the busiest parts of the video, so they over-estimate size
        # slightly; only refuse when there is clearly nothing to gain.
        if best_ratio is not None and best_ratio > 0.95:
            return SearchResult(
                crf=best_crf, vmaf=best_vmaf, met_target=True,
                probes=probes, estimated_ratio=best_ratio, method="vmaf",
                viable=False,
                reason=(
                    f"Reaching VMAF {target_vmaf:.0f} would need roughly "
                    f"{best_ratio * 100:.0f}% of the original's bitrate, so the file "
                    f"would not get meaningfully smaller."
                ),
            )

        return SearchResult(
            crf=best_crf, vmaf=best_vmaf, met_target=True,
            probes=probes, estimated_ratio=best_ratio, method="vmaf",
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)
