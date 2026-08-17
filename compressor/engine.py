"""
Compression orchestration.

Pipeline: probe the source, search for the CRF that hits the quality target,
run the full encode, then verify the result actually got smaller.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .encoders import (
    CodecSpec,
    build_audio_args,
    build_filter_args,
    build_video_args,
    resolve_codec,
)
from .environment import Environment, get_environment
from .probe import ProbeError, SourceInfo, probe
from .quality import NoHeadroom, SearchResult, find_optimal_crf

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# phase, percent within phase, human-readable detail
ProgressFn = Callable[[str, float, str], None]


class CompressionCancelled(RuntimeError):
    """Raised when a job is cancelled through its stop event."""


@dataclass
class CompressionOptions:
    codec: str = "h265"
    target_vmaf: float = 95.0
    speed: str = "quality"           # quality | balanced | fast
    audio: str = "auto"              # auto | copy | none
    max_height: Optional[int] = None
    ten_bit: bool = False
    use_vmaf: bool = True
    # Encode even when the projection says the file will not get smaller.
    force: bool = False
    # Detect scene cuts and give each scene its own CRF, encoded in parallel.
    segmented: bool = False


@dataclass
class CompressionResult:
    source: SourceInfo
    output_path: str
    input_size: int
    output_size: int
    reduction_percent: float
    codec: str
    crf: int
    vmaf: Optional[float]
    met_target: bool
    elapsed_seconds: float
    search: Optional[SearchResult] = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "input_size_mb": round(self.input_size / (1024 * 1024), 2),
            "output_size_mb": round(self.output_size / (1024 * 1024), 2),
            "reduction_percent": round(self.reduction_percent, 1),
            "codec": self.codec,
            "crf": self.crf,
            "vmaf": round(self.vmaf, 2) if self.vmaf else None,
            "met_target": self.met_target,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "source": self.source.summary(),
            "search": self.search.summary() if self.search else None,
            "warnings": self.warnings,
        }


class CompressionEngine:
    """Runs compression jobs. Safe to share across threads."""

    def __init__(self, env: Optional[Environment] = None):
        self.env = env or get_environment()
        # Diagnostics from the most recent segmentation decision, so callers
        # can report why segmenting was or was not attempted.
        self.last_plan = None
        self.last_plan_skipped = ""

    def analyse(self, path: str) -> SourceInfo:
        return probe(path, self.env)

    def compress(
        self,
        input_path: str,
        output_path: str,
        options: Optional[CompressionOptions] = None,
        progress: Optional[ProgressFn] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> CompressionResult:
        options = options or CompressionOptions()
        started = time.monotonic()
        warnings: list[str] = []

        def report(phase: str, percent: float, detail: str = "") -> None:
            if progress:
                progress(phase, percent, detail)

        def check_cancelled() -> None:
            if stop_event and stop_event.is_set():
                raise CompressionCancelled("Cancelled.")

        # ---- 1. analyse the source ----
        report("analyzing", 0.0, "Reading video")
        source = self.analyse(input_path)
        check_cancelled()

        spec = resolve_codec(options.codec, self.env)
        if spec.key != options.codec:
            warnings.append(
                f"{options.codec} is not available in this FFmpeg build; used {spec.label} instead."
            )

        report(
            "analyzing", 100.0,
            f"{source.resolution} {source.video_codec}, {source.waste_label}",
        )

        # ---- 2. find the quality point ----
        if options.use_vmaf and self.env.has_vmaf:
            def search_progress(detail: str, percent: float) -> None:
                check_cancelled()
                report("searching", percent, detail)

            search = find_optimal_crf(
                source, spec, self.env,
                target_vmaf=options.target_vmaf,
                tier=options.speed,
                ten_bit=options.ten_bit,
                progress=search_progress,
            )
        else:
            from .quality import heuristic_crf

            search = heuristic_crf(spec, source)
            if options.use_vmaf and not self.env.has_vmaf:
                warnings.append(
                    "This FFmpeg build lacks libvmaf, so quality was estimated from "
                    "source analysis rather than measured."
                )

        # Stop here if re-encoding cannot help. The sample probes already know
        # the answer, so refusing now costs a minute instead of a full-length
        # encode that ends with a file bigger than the one it started from.
        if not search.viable and not options.force:
            # Suggest the next sensible step down rather than a fixed number. A
            # target above 95 is near-lossless, and on an already-compressed
            # source it is the target itself -- not the video -- that makes the
            # job impossible, so say that instead of blaming the source.
            if options.target_vmaf > 95:
                advice = (
                    f"VMAF {options.target_vmaf:.0f} is a near-lossless target: it asks the "
                    f"encoder to reproduce this source's existing compression artifacts "
                    f"exactly, which costs more bits than the artifacts themselves. "
                    f"Try 95 — visually identical in normal viewing, and where the real "
                    f"savings are."
                )
            elif options.target_vmaf > 90:
                advice = (
                    f"Try a lower quality target (VMAF {options.target_vmaf:.0f} → 90), or "
                    f"switch to AV1, which is more efficient than "
                    f"{source.video_codec.upper()}."
                )
            else:
                advice = (
                    f"Even at VMAF {options.target_vmaf:.0f} there is nothing to gain here. "
                    f"This source is already about as small as it can be."
                )

            raise NoHeadroom(
                f"{search.reason}\n\n"
                f"Keep the original — re-encoding it would produce a larger file and a "
                f"second generation of loss.\n\n{advice}"
            )

        if search.method == "vmaf" and not search.met_target:
            warnings.append(
                f"Could not reach VMAF {options.target_vmaf:.0f} at any tested CRF; "
                f"encoded at CRF {search.crf} (VMAF {search.vmaf:.1f}) because "
                f"compression was forced."
            )

        check_cancelled()
        estimate = ""
        if search.estimated_ratio:
            projected = source.size_bytes * search.estimated_ratio / (1024 * 1024)
            estimate = f" — projecting about {projected:.0f} MB"
        report("searching", 100.0, f"Selected CRF {search.crf}{estimate}")

        # ---- 3. full encode ----
        segments: list = []
        if options.segmented:
            segments = self._encode_segmented(
                source, output_path, spec, search.crf, options,
                report, check_cancelled,
            )
            if not segments:
                # The plan said segmenting would not pay for itself. Fall back
                # to one pass and tell the user why, rather than silently
                # spending the extra time for no benefit.
                if self.last_plan_skipped:
                    warnings.append(
                        f"Skipped scene segmentation: {self.last_plan_skipped}"
                    )
                self._encode(source, output_path, spec, search.crf, options,
                             report, check_cancelled)
        else:
            self._encode(
                source, output_path, spec, search.crf, options,
                report, check_cancelled,
            )

        # ---- 4. verify ----
        out_file = Path(output_path)
        if not out_file.exists() or out_file.stat().st_size == 0:
            raise RuntimeError("Encoding finished but produced no output file.")

        output_size = out_file.stat().st_size
        reduction = ((source.size_bytes - output_size) / source.size_bytes) * 100

        # Last line of defence. The sample-based projection can be wrong on
        # video whose sampled sections are not representative, so verify against
        # the real result and never hand back something bigger than the input.
        if output_size >= source.size_bytes and not options.force:
            growth = (output_size / source.size_bytes - 1) * 100
            out_file.unlink(missing_ok=True)
            raise NoHeadroom(
                f"The re-encode came out {growth:.0f}% larger than the original "
                f"({source.size_bytes / 1024 / 1024:.0f} MB → "
                f"{output_size / 1024 / 1024:.0f} MB), so it has been discarded.\n\n"
                f"This source is already efficiently encoded. Keep the original, or "
                f"lower the quality target to force a smaller file."
            )

        report("done", 100.0, "Complete")

        return CompressionResult(
            source=source,
            output_path=str(out_file),
            input_size=source.size_bytes,
            output_size=output_size,
            reduction_percent=reduction,
            codec=spec.label,
            crf=search.crf,
            vmaf=search.vmaf or None,
            met_target=search.met_target,
            elapsed_seconds=time.monotonic() - started,
            search=search,
            warnings=warnings,
        )

    def _encode_segmented(
        self,
        source: SourceInfo,
        output_path: str,
        spec: CodecSpec,
        baseline_crf: int,
        options: CompressionOptions,
        report: ProgressFn,
        check_cancelled: Callable[[], None],
    ) -> list:
        """
        Per-scene CRF, encoded in parallel, joined by stream copy.

        Returns [] when the video has too few scenes to benefit, letting the
        caller fall back to a single pass.
        """
        from .segmenter import concat_segments, encode_segments, plan_segmentation, worker_count
        from .quality import refine_segment_crf

        report("segmenting", 0.0, "Measuring scene complexity")
        plan = self.last_plan = plan_segmentation(source, self.env)
        check_cancelled()

        # Same rule as the anti-bloat guards: measure before spending CPU, and
        # decline when the measurement says it will not help.
        if not plan.worthwhile:
            self.last_plan_skipped = plan.reason
            return []

        segments = plan.segments
        workers = worker_count(source)
        report("segmenting", 30.0,
               f"{len(segments)} scenes · variation {plan.variation:.2f} · "
               f"{workers} workers")

        # Tune each scene around the baseline. Sequential because each probe
        # already uses every core.
        for position, segment in enumerate(segments, 1):
            check_cancelled()
            if options.use_vmaf and self.env.has_vmaf:
                segment.crf, segment.vmaf = refine_segment_crf(
                    source, spec, self.env,
                    segment.start, segment.duration,
                    baseline_crf, options.target_vmaf,
                    tier=options.speed, ten_bit=options.ten_bit,
                )
            else:
                segment.crf = baseline_crf
            report("segmenting", 30.0 + position / len(segments) * 70.0,
                   f"Tuned scene {position}/{len(segments)} → CRF {segment.crf}")

        workdir = Path(tempfile.mkdtemp(prefix="vc_seg_enc_"))
        try:
            def segment_progress(percent: float, detail: str) -> None:
                report("encoding", percent, detail)

            encode_segments(
                source, segments, spec, options, self.env, workdir,
                progress=segment_progress, workers=workers,
            )
            check_cancelled()
            report("encoding", 99.0, "Joining scenes")
            concat_segments(
                segments, source, output_path,
                build_audio_args(source, options.audio), self.env, workdir,
            )
            report("encoding", 100.0, "Encode complete")
            return segments
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _encode(
        self,
        source: SourceInfo,
        output_path: str,
        spec: CodecSpec,
        crf: int,
        options: CompressionOptions,
        report: ProgressFn,
        check_cancelled: Callable[[], None],
    ) -> None:
        """Run the full-file encode, streaming progress from FFmpeg."""
        cmd = [
            self.env.ffmpeg, "-y", "-hide_banner", "-nostats",
            "-progress", "pipe:1",
            "-i", source.path,
            "-map", "0:v:0",
        ]
        if source.audio is not None and options.audio != "none":
            cmd += ["-map", "0:a:0"]

        cmd += build_video_args(spec, crf, options.speed, source, options.ten_bit)
        cmd += build_filter_args(source, options.max_height)
        cmd += build_audio_args(source, options.audio)
        cmd += ["-movflags", "+faststart", output_path]

        report("encoding", 0.0, f"Encoding with {spec.label} at CRF {crf}")

        # stderr goes to a file rather than a pipe. Encoders such as x265 write
        # a banner and per-frame warnings there; if that pipe filled while this
        # thread was busy reading progress from stdout, FFmpeg would block on
        # the write and the encode would deadlock.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errlog:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=errlog,
                text=True,
                bufsize=1,
                creationflags=_NO_WINDOW,
            )

            speed = ""
            try:
                for line in process.stdout:
                    line = line.strip()

                    if line.startswith(("out_time_us=", "out_time_ms=")):
                        try:
                            # Both keys carry microseconds in current builds.
                            micros = int(line.split("=", 1)[1])
                        except ValueError:
                            continue
                        percent = min((micros / 1_000_000.0) / source.duration * 100, 99.9)
                        report("encoding", max(0.0, percent),
                               f"Encoding at {speed}" if speed else "Encoding")
                    elif line.startswith("speed="):
                        speed = line.split("=", 1)[1].strip()

                    try:
                        check_cancelled()
                    except CompressionCancelled:
                        process.kill()
                        process.wait(timeout=10)
                        Path(output_path).unlink(missing_ok=True)
                        raise
            finally:
                if process.stdout:
                    process.stdout.close()
                process.wait()

            if process.returncode != 0:
                Path(output_path).unlink(missing_ok=True)
                errlog.seek(0)
                tail = [ln for ln in errlog.read().strip().splitlines() if ln][-4:]
                raise RuntimeError(
                    "FFmpeg failed to encode this file.\n" + "\n".join(tail)
                )

        report("encoding", 100.0, "Encode complete")
