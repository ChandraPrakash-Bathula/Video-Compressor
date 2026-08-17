"""
VideoCompressor web app.

Local-first Flask front end over the compression engine. Uploads arrive in
chunks so multi-gigabyte files do not have to be buffered in memory, and each
job runs on a worker thread with a one-at-a-time encode lock, since a video
encode already saturates every core it can get.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from queue import Queue
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from upload_store import UploadStore
from compressor.encoders import hardware_codec
from compressor import (
    CompressionCancelled,
    CompressionEngine,
    CompressionOptions,
    FFmpegNotFound,
    NoHeadroom,
    ProbeError,
    available_codecs,
    get_environment,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_CHUNK_BYTES = 16 * 1024 * 1024          # per-request ceiling
MAX_UPLOAD_BYTES = 32 * 1024 * 1024 * 1024  # 32 GB total per file
JOB_RETENTION_SECONDS = 6 * 60 * 60         # abandoned jobs are swept after 6h

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".asf",
}


@dataclass
class Job:
    """State for one compression, polled by the browser."""

    id: str
    filename: str
    input_path: str
    options: CompressionOptions
    # False when the input is a file the user already had on disk. Such a file
    # is read in place and must never be deleted by cleanup -- it is the user's
    # only copy, not a temporary upload.
    owns_input: bool = True
    # Simple mode: fixed settings, one silent retry at a lower floor before any
    # refusal is ever shown, and a stripped-down result.
    simple: bool = False
    # Disclosure facts for simple mode, surfaced as one line each.
    used_hardware: bool = False
    container_changed: bool = False
    output_suffix: str = ".mp4"
    status: str = "queued"       # queued|running|done|error|cancelled
    phase: str = "queued"        # analyzing|searching|encoding|done
    percent: float = 0.0
    detail: str = "Waiting for an encoder slot"
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    result: Optional[dict] = None
    output_path: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict:
        return {
            "job_id": self.id,
            "filename": self.filename,
            "status": self.status,
            "phase": self.phase,
            "percent": round(self.percent, 1),
            "detail": self.detail,
            "result": self.result,
            "error": self.error,
            "retryable": self.retryable,
            "simple": self.simple,
            "used_hardware": self.used_hardware,
            "container_changed": self.container_changed,
            "elapsed": round(time.time() - self.started, 1) if self.started else 0,
        }


class JobManager:
    """Owns the job table and a single-worker encode queue."""

    def __init__(self, engine: CompressionEngine, workers: int = 1):
        self.engine = engine
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: Queue[str] = Queue()
        for _ in range(workers):
            threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.id] = job
        self.queue.put(job.id)

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status in {"done", "error", "cancelled"}:
            return False
        job.stop_event.set()
        job.status = "cancelled"
        job.detail = "Cancelled"
        return True

    def sweep(self) -> None:
        """Drop finished jobs and their files once they are old enough."""
        cutoff = time.time() - JOB_RETENTION_SECONDS
        with self.lock:
            stale = [
                jid for jid, job in self.jobs.items()
                if job.created < cutoff and job.status in {"done", "error", "cancelled"}
            ]
            for jid in stale:
                job = self.jobs.pop(jid)
                if job.owns_input:
                    _discard(job.input_path)
                _discard(job.output_path)

    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            job = self.get(job_id)
            if job is None or job.stop_event.is_set():
                self.queue.task_done()
                continue
            self._run(job)
            self.queue.task_done()

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.started = time.time()
        output_path = OUTPUT_DIR / f"{job.id}{job.output_suffix}"

        def drop_input() -> None:
            """Delete the source only when this job owns it."""
            if job.owns_input:
                Path(job.input_path).unlink(missing_ok=True)

        def progress(phase: str, percent: float, detail: str) -> None:
            job.phase = phase
            job.detail = detail
            # Weight the phases so the bar advances smoothly overall: analysis
            # 0-5%, quality search 5-25%, encode 25-100%.
            if phase == "analyzing":
                job.percent = percent * 0.05
            elif phase == "searching":
                job.percent = 5 + percent * 0.20
            elif phase == "encoding":
                job.percent = 25 + percent * 0.75
            else:
                job.percent = 100.0

        def run_engine(options):
            return self.engine.compress(
                job.input_path, str(output_path), options,
                progress=progress, stop_event=job.stop_event,
            )

        try:
            try:
                result = run_engine(job.options)
            except NoHeadroom:
                # Simple mode gets one quiet second attempt at a lower floor
                # before the user is told anything. A refusal the user sees
                # should mean the file genuinely has nothing left, not that the
                # first target happened to be ambitious.
                if not job.simple or job.options.target_vmaf <= SIMPLE_VMAF_RETRY:
                    raise
                job.detail = "Trying a lower quality target"
                result = run_engine(
                    replace(job.options, target_vmaf=SIMPLE_VMAF_RETRY)
                )
            job.result = result.summary()
            if job.used_hardware:
                measured = _verify_vmaf(self.engine, job.input_path, str(output_path))
                # Report what was measured. If it came in under the target, the
                # user sees the real figure rather than the calibrated hope.
                job.result["vmaf"] = round(measured, 2) if measured else None
            job.output_path = str(output_path)
            job.status = "done"
            job.phase = "done"
            job.percent = 100.0
            job.detail = f"Reduced by {result.reduction_percent:.1f}%"
            # The source is no longer needed once the encode succeeded.
            drop_input()

        except CompressionCancelled:
            job.status = "cancelled"
            job.detail = "Cancelled"
            drop_input()
            _discard(str(output_path))
        except NoHeadroom as exc:
            # Not an error in the tool -- a real property of the source. Flagged
            # separately so the UI can present it as advice, not a crash. The
            # upload is kept so the user can retry with different settings
            # instead of having to upload the whole file again.
            job.status = "no_headroom"
            job.phase = "done"
            job.percent = 100.0
            job.error = str(exc)
            job.detail = "Already efficiently encoded"
            job.retryable = True
            _discard(str(output_path))
        except (ProbeError, FFmpegNotFound) as exc:
            job.status = "error"
            job.error = str(exc)
            drop_input()
            _discard(str(output_path))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            job.status = "error"
            job.error = str(exc) or "Compression failed unexpectedly."
            drop_input()
            _discard(str(output_path))


# Fixed settings for simple mode. The only thing not fixed is encoder effort,
# which is chosen from the source's measured duration -- see _tier_for_budget.
SIMPLE_VMAF = 95.0
SIMPLE_VMAF_RETRY = 90.0
SIMPLE_TIME_BUDGET_SECONDS = 150

# Fixed search cost and per-second encode cost for a 1080p source, measured on
# this project's own fixtures. Used only to pick a tier, never to report a time.
_TIER_COST = {
    "quality": (30.0, 2.30),
    "balanced": (12.0, 1.30),
    "fast": (6.0, 0.60),
}


# Which containers can carry which codecs, verified by actually muxing each
# combination with this FFmpeg rather than assumed from the specs. QuickTime's
# MOV and Apple's M4V reject AV1 outright -- only MP4 and Matroska take it.
# Anything unlisted is treated as unable, and falls back to MP4 with the change
# disclosed rather than silently applied.
_CONTAINER_SUPPORT = {
    ".mp4": {"h265", "av1", "h264"},
    ".mov": {"h265", "h264"},
    ".mkv": {"h265", "av1", "h264"},
    ".webm": {"av1"},
    ".m4v": {"h265", "h264"},
}


def _output_suffix(source_path: str, codec_family: str) -> tuple[str, bool]:
    """
    Container for the output, matching the input where it legally can.

    Someone who uploads a .mov expects a .mov back; the codec inside is an
    implementation detail. Returns (suffix, fell_back) so a forced change can be
    disclosed rather than silently applied.
    """
    suffix = Path(source_path).suffix.lower()
    if codec_family in _CONTAINER_SUPPORT.get(suffix, set()):
        return suffix, False
    return ".mp4", True


def _hardware_needed(source, budget: int = SIMPLE_TIME_BUDGET_SECONDS) -> bool:
    """True when even fast-tier software cannot finish this source in budget."""
    scale = max(source.pixels / (1920 * 1080), 0.25)
    fixed, rate = _TIER_COST["fast"]
    return fixed + source.duration * rate * scale > budget


def _tier_for_budget(source, budget: int = SIMPLE_TIME_BUDGET_SECONDS) -> str:
    """
    Pick the slowest encoder effort that still fits a wall-clock budget.

    Simple mode promises a result in a couple of minutes, and effort is the only
    dial that moves wall-clock time by more than a rounding error. Rather than
    hardcoding one tier and being wrong for every file that is not the length it
    assumed, this scales the measured cost by duration and pixel count and takes
    the best tier that fits. Long sources fall through to `fast`, which is
    honest: no software encoder returns a ten-minute 1080p video in ninety
    seconds, and pretending otherwise would just move the surprise later.
    """
    scale = max(source.pixels / (1920 * 1080), 0.25)
    for tier in ("quality", "balanced", "fast"):
        fixed, rate = _TIER_COST[tier]
        if fixed + source.duration * rate * scale <= budget:
            return tier
    return "fast"


def _verify_vmaf(engine, source_path: str, output_path: str) -> Optional[float]:
    """
    Measure the quality actually delivered, for encodes that never searched.

    The hardware path uses a fixed calibrated quality parameter instead of a CRF
    search, so nothing has measured this file yet. Calibration says q:v 55 lands
    above VMAF 95 on the fixtures, but this source is not a fixture -- so the
    number reported to the user is measured here, not assumed.

    The same sample windows are cut from both files (identical duration, so
    `_build_reference` picks identical timestamps) and scored with the search's
    own helpers. No new metric logic.
    """
    import tempfile

    from compressor.probe import probe as probe_source
    from compressor.quality import _build_reference, _score

    workdir = Path(tempfile.mkdtemp(prefix="vc_verify_"))
    try:
        source_dir = workdir / "src"
        output_dir = workdir / "out"
        source_dir.mkdir()
        output_dir.mkdir()

        reference = _build_reference(
            probe_source(source_path, engine.env), engine.env, source_dir, 3, 2.0
        )
        distorted = _build_reference(
            probe_source(output_path, engine.env), engine.env, output_dir, 3, 2.0
        )
        if reference is None or distorted is None:
            return None
        return _score(distorted, reference, engine.env, subsample=2)
    except Exception:
        # Verification is a reporting nicety; never fail a finished encode for it.
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _discard(*paths: Optional[str]) -> None:
    for path in paths:
        if path:
            Path(path).unlink(missing_ok=True)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["MAX_CONTENT_LENGTH"] = MAX_CHUNK_BYTES

    # Detect FFmpeg once at startup. A missing install is reported through the
    # UI rather than killing the process, so the page can explain the fix.
    startup_error: Optional[str] = None
    engine: Optional[CompressionEngine] = None
    manager: Optional[JobManager] = None
    best_codec = "h265"
    try:
        engine = CompressionEngine(get_environment())
        manager = JobManager(engine)
        # Decided once here, never per request: prefer AV1 when the installed
        # FFmpeg can produce it, since it is the most efficient codec available.
        available = {spec.key for spec in available_codecs(engine.env)}
        best_codec = "av1" if "av1" in available else (
            "h265" if "h265" in available else "h264"
        )
    except (FFmpegNotFound, RuntimeError) as exc:
        startup_error = str(exc)

    # Recovers any interrupted uploads left on disk by a previous run.
    uploads = UploadStore(UPLOAD_DIR)

    def require_engine():
        if manager is None:
            return jsonify(error=startup_error or "Engine unavailable."), 503
        return None

    @app.route("/")
    def index():
        """The only page: drop a video in, get a smaller one back."""
        return render_template("simple.html")

    @app.route("/api/simple-compress", methods=["POST"])
    def simple_compress():
        """
        Start a job with every setting fixed.

        Deliberately thin: it resolves the same upload the normal path uses,
        hands the same engine a fixed set of options, and returns the same job
        id the existing status endpoint already understands. No compression
        logic lives here.
        """
        if (blocked := require_engine()):
            return blocked

        data = request.get_json(silent=True) or {}
        upload_id = data.get("upload_id")
        local_path = (data.get("path") or "").strip()

        if local_path:
            path = Path(local_path).expanduser()
            try:
                path = path.resolve(strict=True)
            except (OSError, RuntimeError):
                return jsonify(error=f"No such file: {local_path}"), 404
            source_path, filename, owns_input = str(path), path.name, False
        elif upload_id:
            if uploads.get(upload_id) is None:
                return jsonify(error="Unknown upload."), 404
            try:
                upload = uploads.finalize(upload_id)
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            uploads.release(upload_id)
            source_path, filename, owns_input = upload.path, upload.filename, True
        else:
            return jsonify(error="Provide either upload_id or path."), 400

        try:
            source = engine.analyse(source_path)
        except ProbeError as exc:
            if owns_input:
                _discard(source_path)
            return jsonify(error=str(exc)), 400

        # Hardware is a scoped exception (see encoders.HARDWARE_CODECS): it is
        # reached only when even fast software cannot finish this specific file
        # in budget -- projected time, not duration alone.
        hardware = hardware_codec(engine.env) if _hardware_needed(source) else None

        codec_key = hardware.key if hardware else best_codec
        family = "h265" if hardware else best_codec
        suffix, changed = _output_suffix(source_path, family)

        options = CompressionOptions(
            codec=codec_key,
            target_vmaf=SIMPLE_VMAF,
            speed="fast" if hardware else _tier_for_budget(source),
            audio="auto",
            max_height=None,
            ten_bit=False,
            # Hardware encoders take a fixed calibrated quality, so there is no
            # CRF to search for. Quality is verified after the encode instead.
            use_vmaf=not hardware,
            force=bool(data.get("force", False)),
            segmented=not hardware,
        )

        job = Job(
            id=uuid.uuid4().hex,
            filename=filename,
            input_path=source_path,
            options=options,
            owns_input=owns_input,
            simple=True,
            used_hardware=bool(hardware),
            container_changed=changed,
            output_suffix=suffix,
        )
        manager.submit(job)
        manager.sweep()
        return jsonify(job_id=job.id, status=job.status)

    @app.route("/api/capabilities")
    def capabilities():
        """What this machine can do — drives which options the UI offers."""
        if startup_error:
            return jsonify(ok=False, error=startup_error), 503
        env = engine.env
        return jsonify(
            ok=True,
            ffmpeg=env.version,
            vmaf=env.has_vmaf,
            codecs=[
                {"key": c.key, "label": c.label, "note": c.note}
                for c in available_codecs(env)
            ],
        )

    # ---- resumable upload ----
    @app.route("/api/upload/create", methods=["POST"])
    def upload_create():
        if (blocked := require_engine()):
            return blocked

        data = request.get_json(silent=True) or {}
        filename = secure_filename(data.get("filename", ""))
        try:
            total_size = int(data.get("totalSize", 0))
        except (TypeError, ValueError):
            return jsonify(error="Invalid file size."), 400

        if not filename:
            return jsonify(error="Invalid filename."), 400
        if total_size <= 0:
            return jsonify(error="Invalid file size."), 400
        if total_size > MAX_UPLOAD_BYTES:
            limit_gb = MAX_UPLOAD_BYTES // (1024 ** 3)
            return jsonify(error=f"File exceeds the {limit_gb} GB limit."), 400

        suffix = Path(filename).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            return jsonify(
                error=f"{suffix or 'This file type'} is not a supported video format."
            ), 400

        # The original extension is kept so FFmpeg's demuxer probing has the hint.
        upload = uploads.create(filename, total_size, suffix)
        return jsonify(**upload.snapshot())

    @app.route("/api/upload/<upload_id>", methods=["GET"])
    def upload_status(upload_id):
        """Where to resume from. A client that lost its connection asks this first."""
        upload = uploads.get(upload_id)
        if upload is None:
            return jsonify(error="Unknown upload."), 404
        return jsonify(**upload.snapshot())

    @app.route("/api/upload/<upload_id>", methods=["PATCH"])
    def upload_write(upload_id):
        """
        Write one chunk at an explicit byte offset.

        The body is the raw bytes, not multipart: it is streamed straight to
        disk in blocks, so neither the chunk nor the file is ever held whole in
        memory.
        """
        try:
            offset = int(request.headers.get("Upload-Offset", request.args.get("offset", -1)))
        except (TypeError, ValueError):
            return jsonify(error="Missing or invalid Upload-Offset."), 400
        if offset < 0:
            return jsonify(error="Missing or invalid Upload-Offset."), 400

        try:
            upload = uploads.write_chunk(upload_id, offset, request.stream)
        except KeyError:
            return jsonify(error="Unknown upload."), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        return jsonify(**upload.snapshot())

    @app.route("/api/upload/<upload_id>/analyze", methods=["POST"])
    def analyze(upload_id):
        """
        Probe the upload so the UI can preview expected savings.

        Called while bytes are still arriving as well as at the end. A partial
        file often yields resolution and codec immediately, but duration only
        once the index has landed -- which for camera MP4s (moov atom at the
        end) means near the finish. Reported as `partial` until the full
        prediction is available rather than showing a wrong one.
        """
        if (blocked := require_engine()):
            return blocked

        upload = uploads.get(upload_id)
        if upload is None:
            return jsonify(error="Unknown upload."), 404

        try:
            source = engine.analyse(upload.path)
        except ProbeError as exc:
            if not upload.complete:
                return jsonify(ok=True, partial=True, received=upload.received,
                               total=upload.total_size)
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, partial=False, source=source.summary())

    @app.route("/api/local", methods=["POST"])
    def local_file():
        """
        Use a file already on this machine, with no upload at all.

        The fastest transfer is the one that does not happen. Since the server
        binds to localhost and the caller is the machine's own user, a path is
        read in place; nothing is copied.
        """
        if (blocked := require_engine()):
            return blocked

        data = request.get_json(silent=True) or {}
        raw = (data.get("path") or "").strip()
        if not raw:
            return jsonify(error="No path given."), 400

        path = Path(raw).expanduser()
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return jsonify(error=f"No such file: {raw}"), 404
        if not path.is_file():
            return jsonify(error="That path is not a file."), 400
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            return jsonify(error=f"{path.suffix} is not a supported video format."), 400

        try:
            source = engine.analyse(str(path))
        except ProbeError as exc:
            return jsonify(error=str(exc)), 400

        return jsonify(ok=True, partial=False, local=True, path=str(path),
                       filename=path.name, source=source.summary())

    @app.route("/api/compress", methods=["POST"])
    def compress():
        if (blocked := require_engine()):
            return blocked

        data = request.get_json(silent=True) or {}
        upload_id = data.get("upload_id")
        local_path = (data.get("path") or "").strip()

        if local_path:
            # A file the user already has. The job must never delete it.
            path = Path(local_path).expanduser()
            try:
                path = path.resolve(strict=True)
            except (OSError, RuntimeError):
                return jsonify(error=f"No such file: {local_path}"), 404
            if not path.is_file():
                return jsonify(error="That path is not a file."), 400
            source_path, filename, owns_input = str(path), path.name, False
        elif upload_id:
            upload = uploads.get(upload_id)
            if upload is None:
                return jsonify(error="Unknown upload."), 404
            try:
                upload = uploads.finalize(upload_id)
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            # Ownership moves to the job, which is responsible for cleanup.
            uploads.release(upload_id)
            source_path, filename, owns_input = upload.path, upload.filename, True
        else:
            return jsonify(error="Provide either upload_id or path."), 400

        try:
            target_vmaf = float(data.get("target_vmaf", 95))
        except (TypeError, ValueError):
            target_vmaf = 95.0

        max_height = data.get("max_height")
        try:
            max_height = int(max_height) if max_height else None
        except (TypeError, ValueError):
            max_height = None

        options = CompressionOptions(
            codec=str(data.get("codec", "h265")),
            target_vmaf=min(max(target_vmaf, 80.0), 100.0),
            speed=str(data.get("speed", "quality")),
            audio=str(data.get("audio", "auto")),
            max_height=max_height,
            ten_bit=bool(data.get("ten_bit", False)),
            use_vmaf=bool(data.get("use_vmaf", True)),
            force=bool(data.get("force", False)),
            segmented=bool(data.get("segmented", False)),
        )

        job = Job(
            id=uuid.uuid4().hex,
            filename=filename,
            input_path=source_path,
            options=options,
            owns_input=owns_input,
        )
        manager.submit(job)
        manager.sweep()
        uploads.sweep(JOB_RETENTION_SECONDS)
        return jsonify(job_id=job.id, status=job.status)

    @app.route("/api/status/<job_id>")
    def status(job_id):
        if (blocked := require_engine()):
            return blocked
        job = manager.get(job_id)
        if not job:
            return jsonify(error="Unknown job."), 404
        return jsonify(**job.snapshot())

    @app.route("/api/retry/<job_id>", methods=["POST"])
    def retry(job_id):
        """
        Re-run a refused job with new settings.

        The upload is still on disk after a `no_headroom` refusal, so retrying
        with a lower quality target or `force` costs no re-upload.
        """
        if (blocked := require_engine()):
            return blocked

        job = manager.get(job_id)
        if not job or not job.retryable:
            return jsonify(error="This job cannot be retried."), 404
        if not Path(job.input_path).exists():
            return jsonify(error="The source file is no longer available."), 404

        data = request.get_json(silent=True) or {}
        options = replace(
            job.options,
            force=bool(data.get("force", job.options.force)),
            target_vmaf=float(data.get("target_vmaf", job.options.target_vmaf)),
            codec=str(data.get("codec", job.options.codec)),
        )

        retried = Job(
            id=uuid.uuid4().hex,
            filename=job.filename,
            input_path=job.input_path,
            options=options,
        )
        # The retry now owns the source; the old job must not delete it.
        job.retryable = False
        manager.submit(retried)
        return jsonify(job_id=retried.id, status=retried.status)

    @app.route("/api/cancel/<job_id>", methods=["POST"])
    def cancel(job_id):
        if (blocked := require_engine()):
            return blocked
        return jsonify(cancelled=manager.cancel(job_id))

    @app.route("/api/download/<job_id>")
    def download(job_id):
        if (blocked := require_engine()):
            return blocked

        job = manager.get(job_id)
        if not job or job.status != "done" or not job.output_path:
            return jsonify(error="No finished file for this job."), 404
        if not Path(job.output_path).exists():
            return jsonify(error="File is no longer available."), 404

        return send_file(
            job.output_path,
            as_attachment=True,
            download_name=f"{Path(job.filename).stem}_compressed{job.output_suffix}",
            mimetype="video/mp4",
        )

    @app.route("/api/preview/<job_id>")
    def preview(job_id):
        """Serve the result inline so the UI can play it before downloading."""
        if (blocked := require_engine()):
            return blocked
        job = manager.get(job_id)
        if not job or job.status != "done" or not job.output_path:
            return jsonify(error="No finished file for this job."), 404
        return send_file(job.output_path, mimetype="video/mp4", conditional=True)

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error="Upload chunk too large."), 413

    @app.errorhandler(500)
    def server_error(_):
        return jsonify(error="Internal server error."), 500

    return app


def clean_work_dirs() -> None:
    """
    Clear finished outputs from a previous run.

    Uploads are deliberately left alone: an interrupted upload is meant to
    survive a server restart, and UploadStore recovers those on startup and
    sweeps genuinely abandoned ones by age.
    """
    if not OUTPUT_DIR.exists():
        return
    for path in OUTPUT_DIR.iterdir():
        if path.name == ".gitkeep":
            continue
        if path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)


app = create_app()


if __name__ == "__main__":
    clean_work_dirs()
    port = int(os.getenv("PORT", "5001"))
    print(f"\n  VideoCompressor  →  http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
