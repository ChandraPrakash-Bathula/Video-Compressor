"""
VideoCompressor — maximum size reduction at a measured quality floor.

The engine picks its quality setting by measuring the source and testing the
result with VMAF, rather than applying a fixed preset. Typical reductions on
camera and phone footage land in the 80-90% range at VMAF 95, where the
difference is not perceptible in normal viewing.
"""

from .encoders import CODECS, CodecSpec, available_codecs, pick_default_codec
from .engine import (
    CompressionCancelled,
    CompressionEngine,
    CompressionOptions,
    CompressionResult,
)
from .environment import Environment, FFmpegNotFound, get_environment
from .probe import ProbeError, SourceInfo, probe
from .quality import NoHeadroom, SearchResult, find_optimal_crf

__version__ = "2.0.0"

__all__ = [
    "CODECS",
    "CodecSpec",
    "CompressionCancelled",
    "CompressionEngine",
    "CompressionOptions",
    "CompressionResult",
    "Environment",
    "FFmpegNotFound",
    "NoHeadroom",
    "ProbeError",
    "SearchResult",
    "SourceInfo",
    "available_codecs",
    "find_optimal_crf",
    "get_environment",
    "pick_default_codec",
    "probe",
]
