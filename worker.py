import json
import math
import os
import shutil
import socket
import subprocess
import traceback
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from rq import Retry

from cluster_config import (
    RQ_TRANSCRIPT_QUEUE_NAME,
    TASKS_ROOT,
    TRANSCRIPTION_CHUNK_SECONDS,
    TRANSCRIPTION_DISTRIBUTED_ENABLED,
    TRANSCRIPTION_MIN_DISTRIBUTED_SECONDS,
)
from downloader import download_media, get_media_duration, transcribe_with_whisper
from task_store import TaskStore

load_dotenv()

store = TaskStore()
NODE_NAME = socket.gethostname()
TERMINAL_TASK_STATUSES = {"cancelled", "failed", "completed"}
DOWNLOAD_LINKS_LOG_PATH = os.path.abspath(os.getenv("DOWNLOAD_LINKS_LOG_PATH", "download_links.log"))


def log_event(task_id: str, message: str) -> None:
    line = f"[{task_id}] {message}"
    print(line, flush=True)
    store.append_log(task_id, line)


def log_download_link(url: str, file_path: str) -> bool:
    """Append the source URL and the resulting file name to the download log."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    filename = os.path.basename(file_path)
    try:
        with open(DOWNLOAD_LINKS_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{timestamp}\t{filename}\t{url}\n")
        return True
    except OSError as exc:
        print(f"Failed to write download links log {DOWNLOAD_LINKS_LOG_PATH}: {exc}", flush=True)
        return False


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def friendly_status(task: dict, raw_msg: str) -> str:
    is_ru = bool(task.get("is_russian"))
    if "Initializing Whisper" in raw_msg or "Whisper AI" in raw_msg:
        return "Инициализация ИИ..." if is_ru else "Initializing AI..."
    if "Transcribing" in raw_msg:
        return "Распознавание текста..." if is_ru else "Transcribing text..."
    if "Transcription complete" in raw_msg:
        return "Завершено" if is_ru else "Complete"
    if "[download]" in raw_msg:
        return "Загрузка медиа..." if is_ru else "Downloading media..."
    if "[ffmpeg]" in raw_msg or "[ExtractAudio]" in raw_msg or "audio file" in raw_msg:
        return "Подготовка аудио..." if is_ru else "Preparing audio..."
    return raw_msg


def should_distribute(duration: float) -> bool:
    return (
        TRANSCRIPTION_DISTRIBUTED_ENABLED
        and duration >= TRANSCRIPTION_MIN_DISTRIBUTED_SECONDS
        and TRANSCRIPTION_CHUNK_SECONDS > 0
    )


def task_chunks_dir(task_id: str) -> str:
    return os.path.join(TASKS_ROOT, task_id, "chunks")


def task_parts_dir(task_id: str) -> str:
    return os.path.join(TASKS_ROOT, task_id, "parts")


def distributed_output_path(task_id: str, file_path: str) -> str:
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    return os.path.join(TASKS_ROOT, task_id, f"{base_name}_transcript.txt")


def get_task_status(task_id: str) -> str:
    task = store.get_task(task_id) or {}
    return str(task.get("status") or "")


def is_task_terminal(task_id: str) -> bool:
    return get_task_status(task_id) in TERMINAL_TASK_STATUSES


def update_distributed_progress(task_id: str) -> None:
    if is_task_terminal(task_id):
        return
    summary = store.summarize_chunks(task_id)
    if summary["count"] <= 0:
        return
    status = (
        f"Distributed transcription: {summary['completed']}/{summary['count']} chunks complete, "
        f"{summary['processing']} processing, {summary['queued']} queued"
    )
    store.update_task(
        task_id,
        progress=summary["progress"],
        chunk_summary=summary,
        current_status=status,
    )


def update_from_callback(task_id: str, info: dict) -> None:
    task = store.get_task(task_id) or {}
    if info["type"] == "progress":
        store.set_progress(task_id, info.get("percentage", 0), info)
    elif info["type"] == "status":
        raw_msg = info["msg"]
        store.append_log(task_id, raw_msg)
        store.update_task(task_id, current_status=friendly_status(task, raw_msg))


def update_chunk_from_callback(task_id: str, chunk_id: str, info: dict) -> None:
    task = store.get_task(task_id) or {}
    chunk = store.get_chunk(task_id, chunk_id) or {}
    prefix = f"[chunk {chunk.get('index', '?')}]"
    if info["type"] == "progress":
        percentage = round(float(info.get("percentage", 0)), 2)
        store.update_chunk(task_id, chunk_id, progress=percentage, details=info)
        update_distributed_progress(task_id)
    elif info["type"] == "status":
        raw_msg = info["msg"]
        store.append_log(task_id, f"{prefix} {raw_msg}")
        store.update_chunk(task_id, chunk_id, last_status=raw_msg)
        store.update_task(task_id, current_status=friendly_status(task, raw_msg))


def check_cancel(task_id: str) -> bool:
    status = get_task_status(task_id)
    cancelled = status == "cancelled" or store.is_cancelled(task_id)
    if cancelled:
        log_event(task_id, f"Cancellation signal received by worker. status={status}")
    return cancelled


def ensure_active_marker(task_dir: str) -> str:
    active_marker = os.path.join(task_dir, ".active")
    with open(active_marker, "w", encoding="utf-8") as handle:
        handle.write("active")
    return active_marker


def remove_active_marker(active_marker: str) -> None:
    try:
        if os.path.exists(active_marker):
            os.remove(active_marker)
    except Exception:
        pass


def write_final_transcript(output_path: str, segments: list[dict[str, Any]], structured: bool) -> None:
    with open(output_path, "w", encoding="utf-8") as handle:
        if structured:
            for segment in segments:
                text = str(segment.get("text", "")).strip()
                if not text:
                    continue
                handle.write(f"[{format_timestamp(float(segment.get('start', 0) or 0))}] {text}\n")
                if text.endswith((".", "!", "?")):
                    handle.write("\n")
        else:
            first = True
            for segment in segments:
                text = str(segment.get("text", "")).strip()
                if not text:
                    continue
                if not first:
                    handle.write(" ")
                handle.write(text)
                first = False


def split_audio_into_chunks(task_id: str, source_path: str, total_duration: float) -> list[dict[str, Any]]:
    chunks_dir = task_chunks_dir(task_id)
    parts_dir = task_parts_dir(task_id)
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(parts_dir, exist_ok=True)
    pattern = os.path.join(chunks_dir, "chunk_%03d.wav")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        source_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "segment",
        "-segment_time",
        str(TRANSCRIPTION_CHUNK_SECONDS),
        "-c:a",
        "pcm_s16le",
        pattern,
    ]
    log_event(task_id, f"split_audio ffmpeg_cmd={' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg split failed: {result.stderr.strip() or result.stdout.strip()}")
    log_event(task_id, f"split_audio completed stdout_len={len(result.stdout)} stderr_len={len(result.stderr)}")

    chunk_files = sorted(
        os.path.join(chunks_dir, name)
        for name in os.listdir(chunks_dir)
        if name.lower().endswith(".wav")
    )
    if not chunk_files:
        raise RuntimeError("ffmpeg split produced no chunks")

    chunks: list[dict[str, Any]] = []
    offset = 0.0
    for index, chunk_path in enumerate(chunk_files):
        duration = get_media_duration(chunk_path)
        if duration <= 0:
            duration = max(0.0, min(float(TRANSCRIPTION_CHUNK_SECONDS), total_duration - offset))
        chunk_id = f"chunk-{index:03d}"
        chunk_meta = {
            "chunk_id": chunk_id,
            "index": index,
            "path": chunk_path,
            "duration": duration,
            "offset": offset,
            "status": "queued",
            "progress": 0.0,
            "worker_node": None,
            "segments_path": os.path.join(parts_dir, f"{chunk_id}.segments.json"),
            "transcript_path": os.path.join(parts_dir, f"{chunk_id}.txt"),
        }
        chunks.append(chunk_meta)
        log_event(task_id, f"prepared {chunk_id} offset={offset:.2f}s duration={duration:.2f}s path={chunk_path}")
        offset += duration
    return chunks


def run_single_transcription(task_id: str, file_path: str, structured: bool, model_size: str, server_only: bool) -> str:
    duration = get_media_duration(file_path)
    store.update_task(task_id, total_duration=duration, processing_mode="single")
    transcript_path = distributed_output_path(task_id, file_path)
    log_event(task_id, f"single_transcription duration={duration}s transcript_path={transcript_path}")
    transcribe_with_whisper(
        audio_path=file_path,
        output_path=transcript_path,
        structured=structured,
        model_size=model_size,
        total_duration=duration,
        progress_callback=lambda info: update_from_callback(task_id, info),
        check_cancel=lambda: check_cancel(task_id),
    )
    final_path = transcript_path
    if server_only:
        server_path = os.path.join(os.path.dirname(transcript_path), "[SERVER] " + os.path.basename(transcript_path))
        os.replace(transcript_path, server_path)
        final_path = server_path
    return final_path


def enqueue_distributed_transcription(task_id: str, file_path: str, structured: bool, model_size: str, server_only: bool) -> None:
    total_duration = get_media_duration(file_path)
    output_path = distributed_output_path(task_id, file_path)
    store.update_task(
        task_id,
        total_duration=total_duration,
        processing_mode="distributed",
        distributed_output_path=output_path,
        source_file=file_path,
        structured=structured,
        model_size=model_size,
        server_only=server_only,
    )
    log_event(task_id, f"distributed_transcription duration={total_duration:.2f}s chunk_seconds={TRANSCRIPTION_CHUNK_SECONDS}")
    chunks = split_audio_into_chunks(task_id, file_path, total_duration)
    store.save_chunks(task_id, chunks)
    store.update_task(task_id, total_chunks=len(chunks))
    update_distributed_progress(task_id)
    for chunk in chunks:
        job = store.enqueue(
            "worker.process_transcription_chunk",
            task_id,
            chunk["chunk_id"],
            queue_name=RQ_TRANSCRIPT_QUEUE_NAME,
            retry=Retry(max=2, interval=[15, 60]),
        )
        store.update_chunk(task_id, chunk["chunk_id"], rq_job_id=job.id, queue_name=job.origin)
        log_event(task_id, f"enqueued {chunk['chunk_id']} rq_job_id={job.id} queue={job.origin}")
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            log_event(task_id, f"deleted source file after chunk split path={file_path}")
    except Exception as exc:
        log_event(task_id, f"warning failed to delete source file path={file_path}: {exc}")


def finalize_distributed_transcription(task_id: str) -> None:
    task = store.get_task(task_id)
    if not task:
        return
    if str(task.get("status") or "") in TERMINAL_TASK_STATUSES:
        return
    chunks = task.get("chunks", [])
    if not chunks:
        return
    if any(chunk.get("status") == "failed" for chunk in chunks):
        failed_chunks = [chunk["chunk_id"] for chunk in chunks if chunk.get("status") == "failed"]
        raise RuntimeError(f"Chunk transcription failed: {', '.join(failed_chunks)}")
    if any(chunk.get("status") != "completed" for chunk in chunks):
        return

    all_segments: list[dict[str, Any]] = []
    for chunk in chunks:
        segments_path = chunk.get("segments_path")
        if not segments_path or not os.path.exists(segments_path):
            raise RuntimeError(f"Missing segments file for {chunk.get('chunk_id')}")
        with open(segments_path, "r", encoding="utf-8") as handle:
            rel_segments = json.load(handle)
        for segment in rel_segments:
            all_segments.append(
                {
                    "start": float(chunk.get("offset", 0) or 0) + float(segment.get("start", 0) or 0),
                    "end": float(chunk.get("offset", 0) or 0) + float(segment.get("end", 0) or 0),
                    "text": str(segment.get("text", "")).strip(),
                }
            )

    output_path = str(task.get("distributed_output_path") or distributed_output_path(task_id, str(task.get("source_file") or "result")))
    write_final_transcript(output_path, all_segments, bool(task.get("structured", True)))
    final_path = output_path
    if task.get("server_only"):
        server_path = os.path.join(os.path.dirname(output_path), "[SERVER] " + os.path.basename(output_path))
        os.replace(output_path, server_path)
        final_path = server_path
    store.set_status(task_id, "completed", filename=final_path, progress=100, error=None, current_status="Transcription complete.")
    log_event(task_id, f"distributed completed filename={final_path} segments={len(all_segments)}")
    remove_active_marker(os.path.join(TASKS_ROOT, task_id, ".active"))


def try_finalize_distributed_transcription(task_id: str) -> None:
    if not store.acquire_lock(task_id, "finalize"):
        return
    try:
        finalize_distributed_transcription(task_id)
    finally:
        store.release_lock(task_id, "finalize")


def process_uploaded_transcription(task_id: str, file_path: str, structured: bool = True, model_size: str = "base", server_only: bool = False) -> None:
    store.set_status(task_id, "processing", worker_node=NODE_NAME)
    log_event(task_id, f"process_uploaded_transcription worker_node={NODE_NAME} file_path={file_path}")
    task_dir = os.path.dirname(file_path)
    active_marker = ensure_active_marker(task_dir)
    try:
        duration = get_media_duration(file_path)
        store.update_task(task_id, total_duration=duration)
        if should_distribute(duration):
            enqueue_distributed_transcription(task_id, file_path, structured, model_size, server_only)
            log_event(task_id, f"task switched to distributed mode chunks_dir={task_chunks_dir(task_id)}")
            return
        final_path = run_single_transcription(task_id, file_path, structured, model_size, server_only)
        remove_active_marker(active_marker)
        if os.path.exists(file_path):
            os.remove(file_path)
        store.set_status(task_id, "completed", filename=final_path, progress=100, server_only=server_only, error=None)
        log_event(task_id, f"completed filename={final_path}")
    except Exception as exc:
        store.set_status(task_id, "failed", error=str(exc))
        log_event(task_id, f"failed: {exc}")
        log_event(task_id, traceback.format_exc())
        remove_active_marker(active_marker)


def process_transcription_chunk(task_id: str, chunk_id: str) -> None:
    chunk = store.get_chunk(task_id, chunk_id)
    if not chunk:
        raise RuntimeError(f"Chunk not found: {chunk_id}")
    task_status = get_task_status(task_id)
    if task_status == "cancelled":
        store.update_chunk(task_id, chunk_id, status="cancelled", last_status="Skipped because parent task is cancelled.", worker_node=NODE_NAME)
        log_event(task_id, f"chunk skipped chunk_id={chunk_id}: parent task cancelled")
        return
    if task_status == "failed":
        store.update_chunk(task_id, chunk_id, status="failed", error="Skipped because parent task already failed.", last_status="Parent task already failed.", worker_node=NODE_NAME)
        log_event(task_id, f"chunk skipped chunk_id={chunk_id}: parent task already failed")
        return
    if task_status == "completed":
        store.update_chunk(task_id, chunk_id, status="completed", last_status="Skipped because parent task already completed.", worker_node=NODE_NAME)
        log_event(task_id, f"chunk skipped chunk_id={chunk_id}: parent task already completed")
        return
    store.update_chunk(task_id, chunk_id, status="processing", progress=0.0, worker_node=NODE_NAME)
    update_distributed_progress(task_id)
    log_event(task_id, f"process_transcription_chunk chunk_id={chunk_id} worker_node={NODE_NAME} path={chunk.get('path')}")
    try:
        segments = transcribe_with_whisper(
            audio_path=str(chunk["path"]),
            output_path=str(chunk["transcript_path"]),
            structured=False,
            model_size=str((store.get_task(task_id) or {}).get("model_size", "base")),
            total_duration=float(chunk.get("duration", 0) or 0),
            progress_callback=lambda info: update_chunk_from_callback(task_id, chunk_id, info),
            check_cancel=lambda: check_cancel(task_id),
            return_segments=True,
        )
        with open(str(chunk["segments_path"]), "w", encoding="utf-8") as handle:
            json.dump(segments, handle, ensure_ascii=False)
        store.update_chunk(
            task_id,
            chunk_id,
            status="completed",
            progress=100.0,
            worker_node=NODE_NAME,
            segment_count=len(segments),
        )
        log_event(task_id, f"chunk completed chunk_id={chunk_id} segments={len(segments)} worker_node={NODE_NAME}")
        update_distributed_progress(task_id)
        if check_cancel(task_id):
            store.update_chunk(task_id, chunk_id, status="cancelled", last_status="Chunk finished after task cancellation.", worker_node=NODE_NAME)
            update_distributed_progress(task_id)
            return
        if get_task_status(task_id) == "failed":
            log_event(task_id, f"skip finalize for chunk_id={chunk_id}: parent task already failed")
            return
        try:
            try_finalize_distributed_transcription(task_id)
        except Exception as exc:
            log_event(task_id, f"finalize attempt deferred after chunk_id={chunk_id}: {exc}")
            log_event(task_id, traceback.format_exc())
    except Exception as exc:
        if check_cancel(task_id):
            store.update_chunk(task_id, chunk_id, status="cancelled", error=str(exc), worker_node=NODE_NAME)
            log_event(task_id, f"chunk cancelled chunk_id={chunk_id}: {exc}")
            update_distributed_progress(task_id)
            return
        store.update_chunk(task_id, chunk_id, status="failed", error=str(exc), worker_node=NODE_NAME)
        store.set_status(task_id, "failed", error=f"Chunk {chunk_id} failed: {exc}")
        log_event(task_id, f"chunk failed chunk_id={chunk_id}: {exc}")
        log_event(task_id, traceback.format_exc())
        update_distributed_progress(task_id)
        remove_active_marker(os.path.join(TASKS_ROOT, task_id, ".active"))
        raise


def process_remote_media(task_id: str, url: str, quality: str = "720", download_type: str = "video", structured: bool = True, model_size: str = "base", server_only: bool = False) -> None:
    store.set_status(task_id, "processing", worker_node=NODE_NAME)
    task_dir = os.path.join(TASKS_ROOT, task_id)
    os.makedirs(task_dir, exist_ok=True)
    active_marker = ensure_active_marker(task_dir)
    log_event(task_id, f"process_remote_media worker_node={NODE_NAME} url={url} type={download_type} task_dir={task_dir}")

    def update_metadata(info: dict) -> None:
        if "duration" in info:
            store.update_task(task_id, total_duration=info["duration"])
            log_event(task_id, f"source duration={info['duration']}s")

    try:
        effective_type = "audio" if download_type == "transcript" else download_type
        effective_quality = "128" if download_type == "transcript" else quality
        filename = download_media(
            url,
            output_path=task_dir,
            quality=effective_quality,
            media_type=effective_type,
            structured=structured,
            model_size=model_size,
            progress_callback=lambda info: update_from_callback(task_id, info),
            metadata_callback=update_metadata,
            check_cancel=lambda: check_cancel(task_id),
        )
        if not filename or not os.path.exists(filename):
            raise RuntimeError("Download finished without output file")

        if download_type == "transcript":
            duration = get_media_duration(filename)
            if should_distribute(duration):
                enqueue_distributed_transcription(task_id, filename, structured, model_size, server_only)
                log_event(task_id, f"remote transcript switched to distributed mode source={filename}")
                return
            final_path = run_single_transcription(task_id, filename, structured, model_size, server_only)
            remove_active_marker(active_marker)
            try:
                os.remove(filename)
            except Exception:
                pass
            store.set_status(task_id, "completed", filename=final_path, progress=100, server_only=server_only, error=None)
            log_event(task_id, f"completed filename={final_path}")
            return

        remove_active_marker(active_marker)
        final_path = filename
        if server_only:
            server_path = os.path.join(os.path.dirname(filename), "[SERVER] " + os.path.basename(filename))
            os.replace(filename, server_path)
            final_path = server_path
        store.set_status(task_id, "completed", filename=final_path, progress=100, server_only=server_only, error=None)
        if not log_download_link(url, final_path):
            log_event(task_id, f"warning: failed to write download link log path={DOWNLOAD_LINKS_LOG_PATH}")
        log_event(task_id, f"completed filename={final_path}")
    except Exception as exc:
        store.set_status(task_id, "failed", error=str(exc))
        log_event(task_id, f"failed: {exc}")
        log_event(task_id, traceback.format_exc())
        remove_active_marker(active_marker)


def cleanup_expired_task(task_id: str) -> None:
    task_dir = os.path.join(TASKS_ROOT, task_id)
    if os.path.isdir(task_dir):
        shutil.rmtree(task_dir, ignore_errors=True)
    store.delete_task(task_id)
