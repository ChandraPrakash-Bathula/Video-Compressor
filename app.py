import os
import shutil
import uuid
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from video_compressor import VideoCompressor


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

_pending_downloads: dict = {}
_active_uploads: dict = {}
# job_id -> {"status": "compressing"|"done"|"error", "stats": ..., "error": ...}
_jobs: dict = {}


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # 6 MB per chunk

    compressor = VideoCompressor()

    @app.route("/")
    def index():
        return render_template("index.html")

    # ---- chunked upload: start ----
    @app.route("/upload/start", methods=["POST"])
    def upload_start():
        data = request.get_json(silent=True) or {}
        filename = secure_filename(data.get("filename", ""))
        total_size = data.get("totalSize", 0)

        if not filename:
            return jsonify(error="Invalid filename."), 400
        if not total_size or total_size <= 0:
            return jsonify(error="Invalid file size."), 400

        upload_id = uuid.uuid4().hex
        dest = UPLOAD_DIR / f"{upload_id}_{filename}"
        dest.touch()

        _active_uploads[upload_id] = {
            "path": str(dest),
            "filename": filename,
            "total_size": int(total_size),
            "received": 0,
        }
        return jsonify(upload_id=upload_id)

    # ---- chunked upload: send one chunk ----
    @app.route("/upload/chunk/<upload_id>", methods=["POST"])
    def upload_chunk(upload_id):
        info = _active_uploads.get(upload_id)
        if not info:
            return jsonify(error="Unknown upload id."), 404

        chunk = request.files.get("chunk")
        if not chunk:
            return jsonify(error="No chunk data."), 400

        with open(info["path"], "ab") as f:
            while True:
                block = chunk.stream.read(1024 * 1024)
                if not block:
                    break
                f.write(block)

        info["received"] = os.path.getsize(info["path"])
        return jsonify(received=info["received"], total=info["total_size"])

    # ---- chunked upload: finish & start async compress ----
    @app.route("/upload/complete/<upload_id>", methods=["POST"])
    def upload_complete(upload_id):
        info = _active_uploads.pop(upload_id, None)
        if not info:
            return jsonify(error="Unknown upload id."), 404

        temp_input = Path(info["path"])
        if not temp_input.exists():
            return jsonify(error="Uploaded file not found."), 404

        _jobs[upload_id] = {"status": "compressing"}

        def run_compress():
            temp_output = OUTPUT_DIR / f"{upload_id}_compressed.mp4"
            try:
                stats = compressor.smart_compress(
                    input_path=str(temp_input),
                    output_path=str(temp_output),
                    target_reduction=40,
                )
                if stats is None or not temp_output.exists():
                    temp_input.unlink(missing_ok=True)
                    temp_output.unlink(missing_ok=True)
                    _jobs[upload_id] = {
                        "status": "error",
                        "error": "Compression failed. The file may not be a valid "
                                 "video or is already heavily compressed.",
                    }
                    return

                download_name = f"{Path(info['filename']).stem}_compressed.mp4"
                _pending_downloads[upload_id] = {
                    "output": str(temp_output),
                    "input": str(temp_input),
                    "name": download_name,
                }
                _jobs[upload_id] = {
                    "status": "done",
                    "download_id": upload_id,
                    "stats": stats,
                }
            except Exception:
                temp_input.unlink(missing_ok=True)
                temp_output.unlink(missing_ok=True)
                _jobs[upload_id] = {
                    "status": "error",
                    "error": "An unexpected error occurred during processing.",
                }

        threading.Thread(target=run_compress, daemon=True).start()
        return jsonify(job_id=upload_id, status="compressing")

    # ---- poll compression status ----
    @app.route("/status/<job_id>")
    def job_status(job_id):
        job = _jobs.get(job_id)
        if not job:
            return jsonify(error="Unknown job."), 404
        return jsonify(**job)

    @app.route("/download/<download_id>")
    def download(download_id):
        # Clean up the job entry
        _jobs.pop(download_id, None)

        info = _pending_downloads.pop(download_id, None)
        if not info:
            return jsonify(error="Download link expired or invalid."), 404

        output_path = info["output"]
        if not Path(output_path).exists():
            return jsonify(error="File no longer available."), 404

        def cleanup():
            try:
                Path(info["output"]).unlink(missing_ok=True)
                Path(info["input"]).unlink(missing_ok=True)
            except OSError:
                pass

        response = send_file(
            output_path,
            as_attachment=True,
            download_name=info["name"],
            mimetype="video/mp4",
        )
        response.call_on_close(cleanup)
        return response

    @app.errorhandler(400)
    def bad_request(e):
        return jsonify(error=str(e.description)), 400

    @app.errorhandler(413)
    def too_large(e):
        return jsonify(error="Chunk too large."), 413

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify(error="Internal server error. Please try again."), 500

    return app


def clean_work_dirs() -> None:
    _pending_downloads.clear()
    _active_uploads.clear()
    _jobs.clear()
    for folder in (UPLOAD_DIR, OUTPUT_DIR):
        if folder.exists():
            for path in folder.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)


app = create_app()


if __name__ == "__main__":
    clean_work_dirs()
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
