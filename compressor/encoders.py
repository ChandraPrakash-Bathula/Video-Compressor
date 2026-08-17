"""
Encoder definitions and FFmpeg argument construction.

Two rules govern everything here:

1. Quality is set with CRF alone. No `-maxrate`/`-bufsize` cap on the quality
   path -- a bitrate ceiling overrides the rate controller exactly when a scene
   gets complex, which is precisely when the bits are needed. Capping is why
   the old engine produced smeared motion.

2. Encoder presets are slow by default. Preset is nearly free size: `slow`
   versus `ultrafast` is routinely a 2-3x difference in output size at
   identical visual quality, paid for only in CPU time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .environment import Environment
from .probe import SourceInfo

# How hard the encoder works. Quality-first is the default.
SPEED_TIERS = ("quality", "balanced", "fast")


@dataclass(frozen=True)
class CodecSpec:
    """A video codec the engine can target."""

    key: str
    label: str
    encoder: str
    presets: dict[str, str]          # speed tier -> encoder preset
    crf_range: tuple[int, int]       # search bounds for the whole-video probe
    default_crf: int                 # used when VMAF search is unavailable
    # Faster presets used only while searching for the CRF; see probe_preset_for.
    probe_presets: dict[str, str] = field(default_factory=dict)
    # Ceiling for per-scene refinement. Much higher than the global range,
    # because a single easy shot (a locked-off tripod, a fade, a flat
    # background) stays perceptually perfect far past any CRF that would be
    # safe to apply to a whole video. Capping scenes at the global ceiling is
    # what makes segmented encoding pointless -- the easy scenes, which are the
    # only place the savings come from, never get compressed hard enough.
    segment_crf_max: int = 40
    # Hardware encoders take a 1-100 quality scale rather than CRF, and have no
    # speed preset. Both are table facts, not logic.
    quality_flag: str = "-crf"
    hardware: bool = False
    container_tag: Optional[str] = None
    ten_bit_pix_fmt: Optional[str] = None
    extra: Callable[[str], list[str]] = field(default=lambda tier: [])
    note: str = ""

    def preset_for(self, tier: str) -> str:
        return self.presets.get(tier, self.presets["balanced"])

    def probe_preset_for(self, tier: str) -> str:
        """
        Preset for the CRF *search*, which is deliberately faster than the one
        used for the final encode.

        The search only has to decide a number, and the CRF-to-quality mapping
        barely moves with preset. What movement there is runs in the safe
        direction: a slower final encode is slightly better than the probe at
        the same CRF, so the chosen CRF errs toward quality rather than past it.

        Measured on 1080p: one probe at `slow` costs 12.8s against roughly 4s at
        `medium`. Multiplied across five or six probes, this is most of the wait
        before encoding even begins.
        """
        return self.probe_presets.get(tier, self.preset_for(tier))


def _x265_params(tier: str) -> list[str]:
    # aq-mode=3 biases bits toward dark regions, where banding is most visible.
    params = "aq-mode=3:no-sao=1"
    if tier == "quality":
        # Wider motion search and more reference frames: slower, smaller.
        params += ":rd=4:psy-rd=2.0:psy-rdoq=1.0:rdoq-level=2:bframes=8"
    return ["-x265-params", params, "-tag:v", "hvc1"]


def _svtav1_params(tier: str) -> list[str]:
    # tune=0 optimises for subjective quality; the default tune=1 chases PSNR
    # and looks visibly worse at the same bitrate.
    params = "tune=0:enable-overlays=1"
    if tier == "quality":
        params += ":scd=1"
    return ["-svtav1-params", params]


CODECS: dict[str, CodecSpec] = {
    "h265": CodecSpec(
        key="h265",
        label="H.265 / HEVC",
        encoder="libx265",
        presets={"quality": "slow", "balanced": "medium", "fast": "veryfast"},
        probe_presets={"quality": "medium", "balanced": "fast", "fast": "veryfast"},
        crf_range=(18, 34),
        default_crf=26,
        segment_crf_max=46,
        container_tag="hvc1",
        ten_bit_pix_fmt="yuv420p10le",
        extra=_x265_params,
        note="Plays natively on macOS, iOS, Windows 10+, Android 5+. Best balance.",
    ),
    "av1": CodecSpec(
        key="av1",
        label="AV1",
        encoder="libsvtav1",
        presets={"quality": "4", "balanced": "6", "fast": "9"},
        probe_presets={"quality": "6", "balanced": "8", "fast": "9"},
        crf_range=(22, 50),
        default_crf=34,
        segment_crf_max=58,
        ten_bit_pix_fmt="yuv420p10le",
        extra=_svtav1_params,
        note="Smallest files. Slower to encode; needs a recent player.",
    ),
    "h264": CodecSpec(
        key="h264",
        label="H.264 / AVC",
        encoder="libx264",
        presets={"quality": "slow", "balanced": "medium", "fast": "veryfast"},
        probe_presets={"quality": "medium", "balanced": "fast", "fast": "veryfast"},
        crf_range=(18, 30),
        default_crf=23,
        segment_crf_max=40,
        note="Plays on anything ever made. Noticeably larger than H.265.",
    ),
}


# --- Scoped exception to this project's software-only rule -------------------
#
# Everywhere else, hardware encoders are deliberately NOT used: they cost real
# efficiency for speed, which is the wrong trade for a quality-first tool. That
# rule still holds for the CLI and the advanced UI.
#
# The single exception is simple mode, and only for sources so long that even
# fast-tier software encoding cannot finish inside its time budget. There the
# honest choice is a bigger file soon rather than a smaller file far too late,
# and the UI discloses that a software pass would have been smaller.
#
# Do not wire these specs into any other route. A future reader finding the
# software-only rule elsewhere should treat it as absolute outside this block.
#
# The quality value is calibrated, not guessed: measured against this project's
# own fixtures, q:v 55 is the lowest setting at which every one clears VMAF 95
# (high_motion 95.86, screen_record 97.70, animation 99.95). q:v 50 fails on
# high_motion at 93.21.
HARDWARE_QUALITY = 55

HARDWARE_CODECS: dict[str, CodecSpec] = {
    "hevc_videotoolbox": CodecSpec(
        key="hw_h265", label="H.265 (hardware)", encoder="hevc_videotoolbox",
        presets={}, crf_range=(HARDWARE_QUALITY, HARDWARE_QUALITY),
        default_crf=HARDWARE_QUALITY,
        quality_flag="-q:v", hardware=True, container_tag="hvc1",
        extra=lambda tier: ["-tag:v", "hvc1"],
        note="Fast hardware encode; larger than a software pass at equal quality.",
    ),
    "h264_videotoolbox": CodecSpec(
        key="hw_h264", label="H.264 (hardware)", encoder="h264_videotoolbox",
        presets={}, crf_range=(HARDWARE_QUALITY, HARDWARE_QUALITY),
        default_crf=HARDWARE_QUALITY,
        quality_flag="-q:v", hardware=True,
        note="Fast hardware encode; larger than a software pass at equal quality.",
    ),
    "hevc_nvenc": CodecSpec(
        key="hw_nvenc", label="H.265 (hardware)", encoder="hevc_nvenc",
        presets={}, crf_range=(HARDWARE_QUALITY, HARDWARE_QUALITY),
        default_crf=HARDWARE_QUALITY,
        quality_flag="-cq", hardware=True, container_tag="hvc1",
        extra=lambda tier: ["-tag:v", "hvc1"],
        note="Fast hardware encode; larger than a software pass at equal quality.",
    ),
    "hevc_qsv": CodecSpec(
        key="hw_qsv", label="H.265 (hardware)", encoder="hevc_qsv",
        presets={}, crf_range=(HARDWARE_QUALITY, HARDWARE_QUALITY),
        default_crf=HARDWARE_QUALITY,
        quality_flag="-global_quality", hardware=True, container_tag="hvc1",
        extra=lambda tier: ["-tag:v", "hvc1"],
        note="Fast hardware encode; larger than a software pass at equal quality.",
    ),
}


def hardware_codec(env: Environment) -> Optional[CodecSpec]:
    """
    The best hardware encoder this machine actually has, or None.

    Detected at runtime from the encoder list, exactly like the software path --
    no per-OS branching.
    """
    for name in ("hevc_videotoolbox", "hevc_nvenc", "hevc_qsv", "h264_videotoolbox"):
        if env.has_encoder(name):
            return HARDWARE_CODECS[name]
    return None


def available_codecs(env: Environment) -> list[CodecSpec]:
    """Only the codecs this FFmpeg build can actually produce."""
    return [spec for spec in CODECS.values() if env.has_encoder(spec.encoder)]


def pick_default_codec(env: Environment) -> CodecSpec:
    """H.265 when present -- widest playback for the compression it delivers."""
    for key in ("h265", "av1", "h264"):
        spec = CODECS[key]
        if env.has_encoder(spec.encoder):
            return spec
    raise RuntimeError(
        "This FFmpeg build has none of libx265, libsvtav1 or libx264. "
        "Install a standard FFmpeg build."
    )


def resolve_codec(key: str, env: Environment) -> CodecSpec:
    """
    Look up a codec by key, falling back to the default if unavailable.

    Hardware specs resolve here but are deliberately absent from
    `available_codecs`, so they can never be offered by the advanced UI or the
    CLI -- only a caller that names one explicitly can reach them.
    """
    by_key = {spec.key: spec for spec in HARDWARE_CODECS.values()}
    spec = CODECS.get(key) or by_key.get(key)
    if spec is None or not env.has_encoder(spec.encoder):
        return pick_default_codec(env)
    return spec


def build_video_args(
    spec: CodecSpec,
    crf: int,
    tier: str,
    source: SourceInfo,
    ten_bit: bool = False,
    probing: bool = False,
) -> list[str]:
    """
    Video encoding arguments for a given codec and quality point.

    `probing=True` selects the faster search preset. Everything else is held
    identical so the probe measures the same encoder configuration the final
    pass will use.
    """
    args = ["-c:v", spec.encoder, spec.quality_flag, str(crf)]
    if not spec.hardware:
        # Hardware encoders expose no preset; passing one is an error.
        preset = spec.probe_preset_for(tier) if probing else spec.preset_for(tier)
        args += ["-preset", preset]
    args += spec.extra(tier)

    if ten_bit and spec.ten_bit_pix_fmt:
        # 10-bit internal precision reduces banding and is a few percent more
        # efficient even when the source is 8-bit.
        args += ["-pix_fmt", spec.ten_bit_pix_fmt]
    else:
        args += ["-pix_fmt", "yuv420p"]

    return args


def build_audio_args(source: SourceInfo, mode: str = "auto") -> list[str]:
    """
    Audio arguments.

    AAC rather than Opus: Opus inside MP4 has patchy player support, and the
    cross-platform requirement outranks the handful of kilobytes Opus saves.
    Already-small audio is copied rather than re-encoded, since transcoding
    lossy audio only degrades it.
    """
    if source.audio is None or mode == "none":
        return ["-an"]

    if mode == "copy":
        return ["-c:a", "copy"]

    channels = source.audio.channels
    target = 128_000 if channels <= 2 else 256_000

    if mode == "auto":
        already_efficient = source.audio.codec.lower() in {"aac", "opus", "vorbis"}
        if already_efficient and 0 < source.audio.bitrate <= target * 1.1:
            return ["-c:a", "copy"]

    return ["-c:a", "aac", "-b:a", f"{target // 1000}k"]


def build_filter_args(source: SourceInfo, target_height: Optional[int]) -> list[str]:
    """
    Scaling filter, applied only when it does something.

    The old engine ran `scale=trunc(iw/2)*2:trunc(ih/2)*2` on every job, forcing
    a filter pass even when dimensions were already even (they almost always
    are). Skipping it when unnecessary avoids a needless decode-filter-encode
    round trip.
    """
    if target_height and target_height < source.height:
        width = round(source.width * target_height / source.height / 2) * 2
        height = target_height - (target_height % 2)
        return ["-vf", f"scale={width}:{height}:flags=lanczos"]

    if source.width % 2 or source.height % 2:
        return ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]

    return []
