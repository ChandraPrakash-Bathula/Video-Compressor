"""
Locate FFmpeg and discover what this particular build can do.

Nothing in the engine may assume an operating system or a compiled-in encoder.
Everything is detected at runtime so the same code runs on macOS, Windows and
Linux against whatever FFmpeg happens to be installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

# Hide the console window FFmpeg would otherwise flash on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class FFmpegNotFound(RuntimeError):
    """Raised when no usable FFmpeg installation can be located."""


def _candidate_dirs() -> list[Path]:
    """Well-known install locations, searched only if PATH lookup fails."""
    if sys.platform == "win32":
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
            r"C:\\",
        ]
        dirs: list[Path] = []
        for root in filter(None, roots):
            dirs += [
                Path(root) / "ffmpeg" / "bin",
                Path(root) / "FFmpeg" / "bin",
                Path(root) / "Programs" / "ffmpeg" / "bin",
            ]
        dirs.append(Path(r"C:\ffmpeg\bin"))
        return dirs
    return [
        Path("/opt/homebrew/bin"),      # Apple Silicon Homebrew
        Path("/usr/local/bin"),         # Intel Homebrew, manual installs
        Path("/usr/bin"),               # Linux distro packages
        Path("/snap/bin"),
        Path("/var/lib/flatpak/exports/bin"),
    ]


def _locate(tool: str) -> Optional[str]:
    """Find `ffmpeg`/`ffprobe`, honouring explicit env overrides first."""
    override = os.environ.get(f"{tool.upper()}_BINARY")
    if override and Path(override).exists():
        return override

    found = shutil.which(tool)
    if found:
        return found

    exe = f"{tool}.exe" if sys.platform == "win32" else tool
    for directory in _candidate_dirs():
        candidate = directory / exe
        if candidate.exists():
            return str(candidate)

    # imageio-ffmpeg ships a static binary; use it as a last resort. It has no
    # ffprobe, so this only ever rescues the ffmpeg half.
    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return None


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command with no shell, no console window, and text output."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


@dataclass(frozen=True)
class Environment:
    """A validated FFmpeg installation plus its detected capabilities."""

    ffmpeg: str
    ffprobe: str
    version: str
    encoders: frozenset[str] = field(default_factory=frozenset)
    filters: frozenset[str] = field(default_factory=frozenset)

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_filter(self, name: str) -> bool:
        return name in self.filters

    @property
    def has_vmaf(self) -> bool:
        """VMAF drives the quality search; without it we fall back to heuristics."""
        return self.has_filter("libvmaf")

    def describe(self) -> dict:
        """Capability summary for the UI's diagnostics panel."""
        return {
            "ffmpeg": self.ffmpeg,
            "version": self.version,
            "vmaf": self.has_vmaf,
            "encoders": {
                "h264": self.has_encoder("libx264"),
                "h265": self.has_encoder("libx265"),
                "av1": self.has_encoder("libsvtav1"),
            },
        }


def _parse_listing(output: str) -> set[str]:
    """Pull identifiers out of `ffmpeg -encoders` / `-filters` table output."""
    names: set[str] = set()
    for line in output.splitlines():
        # Encoders:  " V....D libx265   H.265 / HEVC"
        # Filters:   " ... libvmaf   VV->V  Calculate the VMAF..."
        match = re.match(r"^\s*[A-Z.]{3,6}\s+([A-Za-z0-9_\-]+)\s", line)
        if match:
            names.add(match.group(1))
    return names


@lru_cache(maxsize=1)
def get_environment() -> Environment:
    """Detect and cache the FFmpeg installation. Raises FFmpegNotFound."""
    ffmpeg = _locate("ffmpeg")
    ffprobe = _locate("ffprobe")

    if not ffmpeg:
        raise FFmpegNotFound(
            "FFmpeg was not found. Install it and make sure it is on your PATH:\n"
            "  macOS    brew install ffmpeg\n"
            "  Windows  winget install Gyan.FFmpeg\n"
            "  Linux    sudo apt install ffmpeg\n"
            "Alternatively set the FFMPEG_BINARY environment variable."
        )
    if not ffprobe:
        raise FFmpegNotFound(
            "Found ffmpeg but not ffprobe. Most packages ship both — install a "
            "complete FFmpeg build, or set the FFPROBE_BINARY environment variable."
        )

    try:
        version_out = run([ffmpeg, "-version"], timeout=30).stdout
        version = version_out.splitlines()[0] if version_out else "unknown"
        encoders = _parse_listing(run([ffmpeg, "-hide_banner", "-encoders"], timeout=30).stdout)
        filters = _parse_listing(run([ffmpeg, "-hide_banner", "-filters"], timeout=30).stdout)
    except (subprocess.SubprocessError, OSError) as exc:
        raise FFmpegNotFound(f"FFmpeg at {ffmpeg} could not be run: {exc}") from exc

    return Environment(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        version=version,
        encoders=frozenset(encoders),
        filters=frozenset(filters),
    )
