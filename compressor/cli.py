"""Command-line interface for the compression engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .encoders import CODECS, available_codecs
from .engine import CompressionEngine, CompressionOptions
from .environment import FFmpegNotFound, get_environment
from .probe import ProbeError
from .quality import NoHeadroom

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".3gp", ".ogv",
}


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class _Progress:
    """Single-line progress bar that stays quiet when not a terminal."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stderr.isatty()
        self.last = ""

    def __call__(self, phase: str, percent: float, detail: str) -> None:
        if not self.enabled:
            return
        weights = {"analyzing": (0, 5), "searching": (5, 25), "encoding": (25, 100)}
        start, end = weights.get(phase, (100, 100))
        overall = start + (end - start) * (percent / 100)

        filled = int(overall / 100 * 28)
        bar = "█" * filled + "░" * (28 - filled)
        line = f"\r  {bar} {overall:5.1f}%  {detail[:48]:<48}"
        if line != self.last:
            sys.stderr.write(line)
            sys.stderr.flush()
            self.last = line

    def done(self) -> None:
        if self.enabled:
            sys.stderr.write("\r" + " " * 90 + "\r")
            sys.stderr.flush()


def _print_source(source) -> None:
    info = source.summary()
    low, high = info["predicted_reduction"]
    print(f"  Source     {info['resolution']} {info['video_codec']} "
          f"@ {info['fps']}fps, {info['duration']}s")
    print(f"  Size       {info['size_mb']} MB "
          f"({info['video_bitrate_kbps']} kbps video)")
    print(f"  Efficiency {info['bpp']:.4f} bpp — {info['waste_label']}")
    print(f"  Expected   {low}-{high}% reduction")


def cmd_info(args) -> int:
    engine = CompressionEngine()
    try:
        source = engine.analyse(args.input)
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(source.summary(), indent=2))
    else:
        print(f"\n{Path(args.input).name}")
        _print_source(source)
        print()
    return 0


def _compress_one(engine: CompressionEngine, src: Path, dst: Path, args) -> bool:
    options = CompressionOptions(
        codec=args.codec,
        target_vmaf=args.vmaf,
        speed=args.speed,
        audio=args.audio,
        max_height=args.max_height,
        ten_bit=args.ten_bit,
        use_vmaf=not args.no_vmaf,
        force=args.even_if_bigger,
        segmented=args.segmented,
    )

    print(f"\n{src.name}")
    try:
        source = engine.analyse(str(src))
    except ProbeError as exc:
        print(f"  error: {exc}", file=sys.stderr)
        return False
    _print_source(source)

    progress = _Progress(not args.quiet)
    try:
        result = engine.compress(str(src), str(dst), options, progress=progress)
    except NoHeadroom as exc:
        # Refusing is the right answer here, not a failure -- report it as
        # advice and keep the exit status clean for batch runs.
        progress.done()
        print(f"  skipped — {exc}".replace("\n\n", "\n             "))
        return False
    except (ProbeError, RuntimeError) as exc:
        progress.done()
        print(f"  error: {exc}", file=sys.stderr)
        return False
    progress.done()

    quality = (f"VMAF {result.vmaf:.1f}" if result.vmaf else "estimated")
    print(f"  Encoded    {result.codec} CRF {result.crf} ({quality})")
    # Growth is reported as growth; "-437% smaller" tells the user nothing.
    delta = (f"{result.reduction_percent:.1f}% smaller" if result.reduction_percent >= 0
             else f"{-result.reduction_percent:.1f}% LARGER")
    print(f"  Result     {_human(result.input_size)} → {_human(result.output_size)}  "
          f"[{delta}]  in {result.elapsed_seconds:.0f}s")
    print(f"  Saved to   {dst}")
    for warning in result.warnings:
        print(f"  note: {warning}")
    return True


def cmd_compress(args) -> int:
    engine = CompressionEngine()
    src = Path(args.input)
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 1

    dst = Path(args.output) if args.output else src.with_name(f"{src.stem}_compressed.mp4")
    if dst.resolve() == src.resolve():
        print("error: output would overwrite the input", file=sys.stderr)
        return 1
    if dst.exists() and not args.force:
        print(f"error: {dst} exists (use --force to overwrite)", file=sys.stderr)
        return 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    return 0 if _compress_one(engine, src, dst, args) else 1


def cmd_batch(args) -> int:
    engine = CompressionEngine()
    in_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)

    if not in_dir.is_dir():
        print(f"error: {in_dir} is not a directory", file=sys.stderr)
        return 1

    videos = sorted(
        p for p in in_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not videos:
        print(f"No video files found in {in_dir}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(videos)} video(s)")

    total_in = total_out = 0
    succeeded = 0
    for index, src in enumerate(videos, 1):
        dst = out_dir / f"{src.stem}_compressed.mp4"
        if dst.exists() and not args.force:
            print(f"\n[{index}/{len(videos)}] {src.name} — skipped (exists)")
            continue
        print(f"\n[{index}/{len(videos)}]", end="")
        if _compress_one(engine, src, dst, args):
            succeeded += 1
            total_in += src.stat().st_size
            total_out += dst.stat().st_size

    if succeeded:
        overall = (total_in - total_out) / total_in * 100
        print(f"\n{succeeded}/{len(videos)} compressed — "
              f"{_human(total_in)} → {_human(total_out)} ({overall:.1f}% smaller)")
    return 0 if succeeded else 1


def cmd_check(args) -> int:
    """Report what this machine's FFmpeg can do."""
    try:
        env = get_environment()
    except FFmpegNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\n  FFmpeg   {env.ffmpeg}")
    print(f"           {env.version}")
    print(f"  VMAF     {'available' if env.has_vmaf else 'MISSING — quality will be estimated'}")
    print("  Codecs")
    for spec in CODECS.values():
        mark = "yes" if env.has_encoder(spec.encoder) else "no "
        print(f"    [{mark}] {spec.label:<16} {spec.note}")
    if not available_codecs(env):
        print("\n  No usable video encoders found.", file=sys.stderr)
        return 1
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="videocompressor",
        description="Shrink videos as far as they go without visible quality loss.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  videocompressor check
  videocompressor info holiday.mov
  videocompressor compress holiday.mov
  videocompressor compress holiday.mov small.mp4 --codec av1
  videocompressor compress clip.mp4 --vmaf 93        # smaller, still excellent
  videocompressor batch ./footage ./compressed
""",
    )
    sub = parser.add_subparsers(dest="command")

    def add_encode_flags(p):
        p.add_argument("--codec", choices=list(CODECS), default="h265",
                       help="target codec (default: h265)")
        p.add_argument("--vmaf", type=float, default=95.0, metavar="N",
                       help="quality floor 80-100; 95 is visually lossless (default: 95)")
        p.add_argument("--speed", choices=("quality", "balanced", "fast"),
                       default="quality", help="encoder effort (default: quality)")
        p.add_argument("--audio", choices=("auto", "copy", "none"), default="auto",
                       help="audio handling (default: auto)")
        p.add_argument("--max-height", type=int, default=None, metavar="PX",
                       help="downscale if taller, e.g. 1080")
        p.add_argument("--ten-bit", action="store_true",
                       help="encode 10-bit: slightly smaller, less banding")
        p.add_argument("--no-vmaf", action="store_true",
                       help="skip the quality search and use source heuristics")
        p.add_argument("--force", action="store_true", help="overwrite existing output")
        p.add_argument("--even-if-bigger", action="store_true",
                       help="encode even when the result will not be smaller")
        p.add_argument("--segmented", action="store_true",
                       help="per-scene CRF, encoded in parallel; measures first and "
                            "falls back to a single pass when it will not help")
        p.add_argument("--quiet", action="store_true", help="no progress bar")

    p_compress = sub.add_parser("compress", help="compress a single video")
    p_compress.add_argument("input")
    p_compress.add_argument("output", nargs="?")
    add_encode_flags(p_compress)
    p_compress.set_defaults(func=cmd_compress)

    p_batch = sub.add_parser("batch", help="compress every video in a folder")
    p_batch.add_argument("input_dir")
    p_batch.add_argument("output_dir")
    add_encode_flags(p_batch)
    p_batch.set_defaults(func=cmd_batch)

    p_info = sub.add_parser("info", help="analyse a video without compressing")
    p_info.add_argument("input")
    p_info.add_argument("--json", action="store_true")
    p_info.set_defaults(func=cmd_info)

    p_check = sub.add_parser("check", help="show FFmpeg capabilities")
    p_check.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return args.func(args)
    except FFmpegNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
