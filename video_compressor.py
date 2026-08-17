#!/usr/bin/env python3
"""
Command-line entry point.

    python video_compressor.py check
    python video_compressor.py info clip.mov
    python video_compressor.py compress clip.mov
    python video_compressor.py batch ./footage ./compressed

The implementation lives in the `compressor` package; this file just forwards
to it so the historical `python video_compressor.py ...` invocation keeps
working.
"""

import sys

from compressor.cli import main

if __name__ == "__main__":
    sys.exit(main())
