"""
Resumable, offset-addressed uploads.

A tus-equivalent: the client asks where to resume, then writes chunks at an
explicit byte offset. Three properties matter and each is load-bearing for
multi-gigabyte files.

*Preallocated destination.* The file is created at full size up front, so
chunks are written with seek+write rather than append. Out-of-order arrival
needs no buffering or reassembly pass.

*Persisted manifest.* Received byte ranges are written to disk beside the file,
so an upload survives a server restart, not just a dropped socket.

*Streamed writes.* Chunks are copied straight from the request stream to the
file in fixed blocks; a whole chunk is never held in memory, let alone a whole
file.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional

BLOCK = 1024 * 1024          # streaming copy granularity
MANIFEST_SUFFIX = ".upload.json"


@dataclass
class Upload:
    """One in-progress or completed upload."""

    id: str
    filename: str
    total_size: int
    path: str
    # Half-open [start, end) byte ranges already on disk, kept sorted and merged.
    ranges: list[list[int]] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    @property
    def received(self) -> int:
        return sum(end - start for start, end in self.ranges)

    @property
    def offset(self) -> int:
        """
        Length of the contiguous prefix already stored.

        This is what a resuming client should continue from. It is not the same
        as `received` when chunks arrived out of order.
        """
        return self.ranges[0][1] if self.ranges and self.ranges[0][0] == 0 else 0

    @property
    def complete(self) -> bool:
        return self.offset >= self.total_size

    def add_range(self, start: int, end: int) -> None:
        """Insert a range, coalescing with any it touches or overlaps."""
        merged: list[list[int]] = []
        placed = False
        for existing in sorted(self.ranges + [[start, end]]):
            if merged and existing[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], existing[1])
            else:
                merged.append(list(existing))
            placed = True
        self.ranges = merged if placed else self.ranges

    def snapshot(self) -> dict:
        return {
            "upload_id": self.id,
            "filename": self.filename,
            "total_size": self.total_size,
            "received": self.received,
            "offset": self.offset,
            "complete": self.complete,
        }


class UploadStore:
    """Thread-safe registry of resumable uploads backed by a directory."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._uploads: dict[str, Upload] = {}
        self._load_existing()

    # ---- persistence ----

    def _manifest_path(self, upload_id: str) -> Path:
        return self.dir / f"{upload_id}{MANIFEST_SUFFIX}"

    def _save(self, upload: Upload) -> None:
        self._manifest_path(upload.id).write_text(json.dumps({
            "id": upload.id,
            "filename": upload.filename,
            "total_size": upload.total_size,
            "path": upload.path,
            "ranges": upload.ranges,
            "created": upload.created,
        }))

    def _load_existing(self) -> None:
        """Recover interrupted uploads so a restart does not lose progress."""
        for manifest in self.dir.glob(f"*{MANIFEST_SUFFIX}"):
            try:
                data = json.loads(manifest.read_text())
                upload = Upload(
                    id=data["id"],
                    filename=data["filename"],
                    total_size=int(data["total_size"]),
                    path=data["path"],
                    ranges=[list(r) for r in data.get("ranges", [])],
                    created=float(data.get("created", time.time())),
                )
            except (OSError, ValueError, KeyError):
                manifest.unlink(missing_ok=True)
                continue

            if Path(upload.path).exists():
                self._uploads[upload.id] = upload
            else:
                manifest.unlink(missing_ok=True)

    # ---- lifecycle ----

    def create(self, filename: str, total_size: int, suffix: str) -> Upload:
        upload_id = uuid.uuid4().hex
        destination = self.dir / f"{upload_id}{suffix}"

        # Preallocate so chunks can be written at arbitrary offsets. truncate()
        # creates a sparse file on APFS/NTFS/ext4 — no upfront write cost.
        with open(destination, "wb") as handle:
            handle.truncate(total_size)

        upload = Upload(
            id=upload_id,
            filename=filename,
            total_size=total_size,
            path=str(destination),
        )
        with self._lock:
            self._uploads[upload_id] = upload
            self._save(upload)
        return upload

    def get(self, upload_id: str) -> Optional[Upload]:
        with self._lock:
            return self._uploads.get(upload_id)

    def write_chunk(self, upload_id: str, offset: int, stream: BinaryIO) -> Upload:
        """
        Stream one chunk to its byte offset.

        Raises KeyError for an unknown upload and ValueError for an offset that
        would write past the declared size.
        """
        upload = self.get(upload_id)
        if upload is None:
            raise KeyError(upload_id)
        if offset < 0 or offset > upload.total_size:
            raise ValueError(f"Offset {offset} is outside the declared file size.")

        written = 0
        with open(upload.path, "r+b") as handle:
            handle.seek(offset)
            while True:
                block = stream.read(BLOCK)
                if not block:
                    break
                if offset + written + len(block) > upload.total_size:
                    raise ValueError("Chunk would write past the declared file size.")
                handle.write(block)
                written += len(block)

        if written:
            with self._lock:
                upload.add_range(offset, offset + written)
                self._save(upload)
        return upload

    def finalize(self, upload_id: str) -> Upload:
        """Verify the upload is whole and drop its manifest."""
        upload = self.get(upload_id)
        if upload is None:
            raise KeyError(upload_id)
        if not upload.complete:
            raise ValueError(
                f"Upload is incomplete: {upload.offset} of {upload.total_size} bytes."
            )

        actual = os.path.getsize(upload.path)
        if actual != upload.total_size:
            raise ValueError(
                f"Uploaded file is {actual} bytes but {upload.total_size} were declared."
            )

        self._manifest_path(upload_id).unlink(missing_ok=True)
        return upload

    def discard(self, upload_id: str) -> None:
        with self._lock:
            upload = self._uploads.pop(upload_id, None)
        if upload:
            Path(upload.path).unlink(missing_ok=True)
            self._manifest_path(upload_id).unlink(missing_ok=True)

    def release(self, upload_id: str) -> None:
        """Forget an upload without deleting its file (ownership moved to a job)."""
        with self._lock:
            self._uploads.pop(upload_id, None)
        self._manifest_path(upload_id).unlink(missing_ok=True)

    def sweep(self, max_age_seconds: float) -> None:
        """Delete uploads that were abandoned before completing."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            stale = [
                uid for uid, upload in self._uploads.items()
                if upload.created < cutoff and not upload.complete
            ]
        for uid in stale:
            self.discard(uid)
