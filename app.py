import builtins
import os
import shutil
import threading
import time
import uuid
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from cluster_config import (
    APP_PORT,
    CLEANUP_INTERVAL_SECONDS,
    DEFAULT_VIDEO_QUALITY,
    ENABLE_SAVE_ON_SERVER,
    FILE_EXPIRATION_SECONDS,
    RQ_DEFAULT_QUEUE_NAME,
    RQ_PORNHUB_QUEUE_NAME,
    RQ_TRANSCRIPT_QUEUE_NAME,
    SHARED_STORAGE_ROOT,
    TASK_STALL_TIMEOUT_SECONDS,
    TASKS_ROOT,
)
from task_store import TaskStore


def safe_print(*args, **kwargs):
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = [str(arg).encode("ascii", errors="replace").decode("ascii") for arg in args]
        builtins.print(*safe_args, **kwargs)


print = safe_print
load_dotenv()

app = Flask(__name__)
app.secret_key = "supersecretkey"
store = TaskStore()


def log_event(scope: str, message: str) -> None:
    print(f"[{scope}] {message}")


def summarize_headers() -> str:
    interesting = ["Host", "Content-Type", "Content-Length", "Accept-Language", "User-Agent", "X-Forwarded-For", "X-Real-IP"]
    parts = []
    for key in interesting:
        value = request.headers.get(key)
        if value:
            parts.append(f"{key}={value}")
    return ", ".join(parts) if parts else "no-interesting-headers"


def task_dir(task_id: str) -> str:
    return os.path.join(TASKS_ROOT, task_id)


def is_pornhub_url(url: str | None) -> bool:
    """Accept PornHub's supported primary domains and their subdomains only."""
    if not url:
        return False
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    return hostname in {"pornhub.com", "pornhub.org"} or hostname.endswith((".pornhub.com", ".pornhub.org"))


def remove_active_marker(task_id: str) -> None:
    """Allow terminal tasks to expire from the shared task directory."""
    try:
        os.remove(os.path.join(task_dir(task_id), ".active"))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_event("cleanup", f"failed to remove active marker task_id={task_id}: {exc}")


def mark_stalled_task_failed(task_id: str, task: dict, now: float) -> None:
    updated_at = int(task.get("updated_at", 0) or 0)
    stalled_for = max(0, int(now) - updated_at)
    error = f"Task stopped updating for {stalled_for}s (watchdog timeout: {TASK_STALL_TIMEOUT_SECONDS}s)"
    store.set_status(task_id, "failed", error=error, current_status="Task failed: worker stopped reporting progress.")
    store.append_log(task_id, f"[{task_id}] {error}; marked failed by watchdog")
    remove_active_marker(task_id)
    # Retain the error response for the normal expiration period instead of
    # deleting the task directory in the same cleanup pass.
    try:
        os.utime(task_dir(task_id), None)
    except OSError as exc:
        log_event("cleanup", f"failed to refresh stalled task directory task_id={task_id}: {exc}")
    log_event("watchdog", f"marked stalled task failed task_id={task_id} stalled_for={stalled_for}s")


def reconcile_distributed_task(task_id: str) -> dict | None:
    task = store.get_task(task_id)
    if not task or task.get("processing_mode") != "distributed" or task.get("status") != "processing":
        return task
    changed = False
    for chunk in task.get("chunks", []):
        chunk_status = chunk.get("status")
        if chunk_status not in {"queued", "processing"}:
            continue
        rq_status = store.get_job_status(chunk.get("rq_job_id"))
        if rq_status in {"failed", "stopped", "canceled"}:
            store.update_chunk(
                task_id,
                chunk["chunk_id"],
                status="failed",
                error=f"RQ job entered terminal state: {rq_status}",
                last_status=f"RQ terminal state: {rq_status}",
            )
            changed = True
            continue
        if rq_status is None and int(chunk.get("updated_at", 0) or 0) < int(time.time()) - 120:
            store.update_chunk(
                task_id,
                chunk["chunk_id"],
                status="failed",
                error="RQ job disappeared from Redis",
                last_status="RQ job disappeared from Redis",
            )
            changed = True
    if not changed:
        return task
    summary = store.summarize_chunks(task_id)
    if summary["failed"] > 0:
        failed_ids = [chunk["chunk_id"] for chunk in store.get_chunks(task_id) if chunk.get("status") == "failed"]
        store.set_status(
            task_id,
            "failed",
            progress=summary["progress"],
            chunk_summary=summary,
            current_status=f"Distributed transcription failed on chunks: {', '.join(failed_ids)}",
            error=f"Distributed transcription failed on chunks: {', '.join(failed_ids)}",
        )
    else:
        store.update_task(
            task_id,
            progress=summary["progress"],
            chunk_summary=summary,
            current_status=(
                f"Distributed transcription: {summary['completed']}/{summary['count']} chunks complete, "
                f"{summary['processing']} processing, {summary['queued']} queued"
            ),
        )
    return store.get_task(task_id)


def calculate_eta(task: dict | None) -> int | None:
    if not task or task.get("status") != "processing":
        return None
    total_duration = float(task.get("total_duration", 0) or 0)
    progress = float(task.get("progress", 0) or 0)
    if total_duration <= 0 or progress <= 0 or progress >= 100 or task.get("download_type") != "transcript":
        return None
    model_size = task.get("model_size", "base")
    if model_size == "small":
        factor = 0.6
    elif model_size == "base":
        factor = 0.25
    else:
        factor = 0.1
    remaining_video_sec = total_duration * (1 - progress / 100.0)
    return max(1, int(remaining_video_sec * factor / 60))


def cleanup_downloads() -> None:
    while True:
        try:
            os.makedirs(TASKS_ROOT, exist_ok=True)
            now = time.time()
            for item in os.listdir(TASKS_ROOT):
                item_path = os.path.join(TASKS_ROOT, item)
                try:
                    task = store.get_task(item)
                    if task and task.get("status") in {"queued", "processing"}:
                        updated_at = int(task.get("updated_at", 0) or 0)
                        if now - updated_at > TASK_STALL_TIMEOUT_SECONDS:
                            mark_stalled_task_failed(item, task, now)
                            continue
                        log_event("cleanup", f"skip active task_id={item} status={task.get('status')}")
                        continue
                    if task and task.get("status") == "cancelled":
                        remove_active_marker(item)
                    if os.path.isdir(item_path) and os.path.exists(os.path.join(item_path, ".active")):
                        log_event("cleanup", f"skip task_id={item} because .active exists")
                        continue
                    if now - os.path.getmtime(item_path) <= FILE_EXPIRATION_SECONDS:
                        continue
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path, ignore_errors=True)
                    elif os.path.isfile(item_path):
                        os.remove(item_path)
                    if task:
                        store.delete_task(item)
                    log_event("cleanup", f"deleted expired task_id={item} path={item_path}")
                except Exception as exc:
                    log_event("cleanup", f"error while deleting {item_path}: {exc}")
        except Exception as exc:
            log_event("cleanup", f"loop failure: {exc}")
        time.sleep(CLEANUP_INTERVAL_SECONDS)


cleanup_thread = threading.Thread(target=cleanup_downloads, daemon=True, name="shared-cleanup")
cleanup_thread.start()


def normalize_uploaded_filename(file) -> str:
    orig_filename = secure_filename(file.filename or "")
    if not orig_filename:
        orig_filename = "uploaded_file"
    _, ext = os.path.splitext(orig_filename)
    if ext:
        return orig_filename
    mime_map = {
        "audio/x-m4a": ".m4a",
        "audio/m4a": ".m4a",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/aac": ".aac",
        "audio/flac": ".flac",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
    }
    if orig_filename.lower() in ["m4a", "mp3", "mp4", "wav", "ogg", "aac", "flac", "avi", "mkv", "mov"]:
        return "uploaded_file." + orig_filename.lower()
    if file.content_type in mime_map:
        return orig_filename + mime_map[file.content_type]
    return orig_filename


@app.route("/")
def index():
    return render_template(
        "index.html",
        enable_save_on_server=ENABLE_SAVE_ON_SERVER,
        default_quality=DEFAULT_VIDEO_QUALITY,
    )


@app.route("/start", methods=["POST"])
def start_download():
    log_event("HTTP", f"POST /start remote_addr={request.remote_addr} is_json={request.is_json} headers=[{summarize_headers()}]")
    if request.is_json:
        url = request.json.get("url")
        file = None
        quality = request.json.get("quality", DEFAULT_VIDEO_QUALITY)
        download_type = request.json.get("download_type", "video")
        structured = request.json.get("structured", True)
        model_size = request.json.get("model_size", "base")
        server_only = request.json.get("server_only", False)
        publish_to_telegram = request.json.get("publish_to_telegram", False)
    else:
        url = request.form.get("url")
        file = request.files.get("file")
        quality = request.form.get("quality", DEFAULT_VIDEO_QUALITY)
        download_type = request.form.get("download_type", "video")
        structured = request.form.get("structured", "true").lower() == "true"
        model_size = request.form.get("model_size", "base")
        server_only = request.form.get("server_only", "false").lower() == "true"
        publish_to_telegram = request.form.get("publish_to_telegram", "false").lower() == "true"

    publish_to_telegram = bool(publish_to_telegram)
    is_pornhub_video = is_pornhub_url(url) and download_type == "video"
    if publish_to_telegram and not is_pornhub_video:
        return jsonify({"error": "Telegram publishing is available only for PornHub video downloads"}), 400
    # Telegram needs the server-side file as its upload source.  Do not also
    # redirect the browser to download the completed video.
    if publish_to_telegram:
        server_only = True

    log_event("HTTP", f"Parsed start payload url_present={bool(url)} file_present={bool(file)} quality={quality} download_type={download_type} structured={structured} model_size={model_size} server_only={server_only} publish_to_telegram={publish_to_telegram}")
    if not url and not file:
        return jsonify({"error": "URL or File is required"}), 400

    task_id = str(uuid.uuid4())
    os.makedirs(task_dir(task_id), exist_ok=True)
    with open(os.path.join(task_dir(task_id), ".active"), "w", encoding="utf-8") as handle:
        handle.write("active")

    task = {
        "status": "queued",
        "progress": 0,
        "filename": None,
        "error": None,
        "details": {},
        "server_only": server_only,
        "is_russian": "ru" in request.headers.get("Accept-Language", "").lower(),
        "structured": structured,
        "model_size": model_size,
        "download_type": download_type,
        "url": url,
        "worker_node": None,
        "publish_to_telegram": publish_to_telegram,
        "telegram_status": "requested" if publish_to_telegram else "not_requested",
    }
    store.save_task(task_id, task)
    store.append_log(task_id, f"[{task_id}] Task created on API node remote_addr={request.remote_addr}")

    if file:
        filename = normalize_uploaded_filename(file)
        file_path = os.path.join(task_dir(task_id), filename)
        store.append_log(task_id, f"[{task_id}] Saving uploaded file filename={filename} content_type={file.content_type} content_length={request.content_length}")
        file.save(file_path)
        saved_size = os.path.getsize(file_path) if os.path.exists(file_path) else -1
        store.append_log(task_id, f"[{task_id}] Uploaded file saved path={file_path} size={saved_size} bytes")
        job = store.enqueue(
            "worker.process_uploaded_transcription",
            task_id,
            file_path,
            queue_name=RQ_TRANSCRIPT_QUEUE_NAME,
            structured=structured,
            model_size=model_size,
            server_only=server_only,
        )
    else:
        if download_type == "transcript":
            queue_name = RQ_TRANSCRIPT_QUEUE_NAME
        elif is_pornhub_url(url):
            queue_name = RQ_PORNHUB_QUEUE_NAME
        else:
            queue_name = RQ_DEFAULT_QUEUE_NAME
        job = store.enqueue(
            "worker.process_remote_media",
            task_id,
            url,
            queue_name=queue_name,
            quality=quality,
            download_type=download_type,
            structured=structured,
            model_size=model_size,
            server_only=server_only,
        )
    store.update_task(task_id, rq_job_id=job.id, queue_name=job.origin)
    store.append_log(task_id, f"[{task_id}] Enqueued rq_job_id={job.id} queue={job.origin}")
    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def get_progress(task_id: str):
    task = reconcile_distributed_task(task_id)
    if not task:
        log_event("HTTP", f"GET /progress/{task_id} -> 404 task not found")
        return jsonify({"error": "Task not found"}), 404
    eta = calculate_eta(task)
    if eta is not None:
        task["eta_minutes"] = eta
    log_event(task_id, f"GET /progress status={task.get('status')} progress={task.get('progress')} filename={task.get('filename')} error={task.get('error')}")
    return jsonify(task)


@app.route("/cancel/<task_id>", methods=["POST"])
def cancel_task(task_id: str):
    if store.cancel_task(task_id):
        remove_active_marker(task_id)
        store.append_log(task_id, f"[{task_id}] Task cancelled via HTTP")
        return jsonify({"success": True})
    log_event("HTTP", f"POST /cancel/{task_id} -> 404 task not found")
    return jsonify({"error": "Task not found"}), 404


@app.route("/get_file/<task_id>")
def get_file(task_id: str):
    task = store.get_task(task_id)
    if not task or task.get("status") != "completed" or not task.get("filename"):
        log_event("HTTP", f"GET /get_file/{task_id} -> 404 status={task.get('status') if task else None} filename={task.get('filename') if task else None}")
        return "File not ready or task not found", 404
    return send_file(task["filename"], as_attachment=True)


if __name__ == "__main__":
    os.makedirs(TASKS_ROOT, exist_ok=True)
    os.makedirs(SHARED_STORAGE_ROOT, exist_ok=True)
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=APP_PORT)
