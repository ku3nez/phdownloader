"""Playlist / series downloads: one parent task, one RQ job per episode.

The parent task keeps the episodes as its "chunks" (the same Redis hash the
distributed transcription uses), so the page can draw one bar per episode.
"""

import os
import time

from cluster_config import TASKS_ROOT

BATCH_MODE = "batch"
ITEM_TERMINAL = {"completed", "failed", "cancelled"}
BATCH_FILE_EXPIRATION_SECONDS = int(os.getenv("BATCH_FILE_EXPIRATION_SECONDS", 3 * 60 * 60))
MAX_BATCH_ITEMS = int(os.getenv("PLAYLIST_MAX_ITEMS", "300"))


def summary(chunks):
    counts = {"count": len(chunks), "completed": 0, "failed": 0, "cancelled": 0, "processing": 0, "queued": 0}
    progress = 0.0
    for chunk in chunks:
        state = chunk.get("status") or "queued"
        counts[state if state in counts else "queued"] += 1
        progress += 100.0 if state in ITEM_TERMINAL else float(chunk.get("progress") or 0)
    counts["progress"] = round(progress / len(chunks), 2) if chunks else 0
    return counts


def status_text(counts, russian):
    if russian:
        return (f"Скачано {counts['completed']} из {counts['count']}"
                + (f", качается {counts['processing']}" if counts["processing"] else "")
                + (f", ошибок {counts['failed']}" if counts["failed"] else ""))
    return (f"Downloaded {counts['completed']} of {counts['count']}"
            + (f", {counts['processing']} running" if counts["processing"] else "")
            + (f", {counts['failed']} failed" if counts["failed"] else ""))


def refresh(store, task_id):
    """Update the parent's progress; finish it once every episode is done."""
    task = store.get_task(task_id)
    if not task or task.get("processing_mode") != BATCH_MODE:
        return task
    chunks = task.get("chunks") or []
    counts = summary(chunks)
    russian = bool(task.get("is_russian"))
    if task.get("status") in ITEM_TERMINAL:
        return task
    if counts["count"] and all((c.get("status") or "queued") in ITEM_TERMINAL for c in chunks):
        if not store.acquire_lock(task_id, "batch-finish"):
            return task
        try:
            if counts["completed"]:
                store.set_status(task_id, "completed", progress=100, chunk_summary=counts, error=None,
                                 current_status=status_text(counts, russian))
            else:
                errors = [c.get("error") for c in chunks if c.get("error")]
                store.set_status(task_id, "failed", progress=100, chunk_summary=counts,
                                 error=errors[0] if errors else "Nothing was downloaded",
                                 current_status=status_text(counts, russian))
            try:
                os.remove(os.path.join(TASKS_ROOT, task_id, ".active"))
            except Exception:
                pass
        finally:
            store.release_lock(task_id, "batch-finish")
    else:
        store.update_task(task_id, progress=counts["progress"], chunk_summary=counts,
                          current_status=status_text(counts, russian))
    return store.get_task(task_id)


def reconcile(store, task_id):
    """Mark episodes whose RQ job died as failed (API side, like distributed)."""
    task = store.get_task(task_id)
    if not task or task.get("processing_mode") != BATCH_MODE or task.get("status") != "processing":
        return task
    for chunk in task.get("chunks") or []:
        if chunk.get("status") not in {"queued", "processing"} or not chunk.get("rq_job_id"):
            continue
        state = store.get_job_status(chunk["rq_job_id"])
        if state in {"failed", "stopped", "canceled"} or (
                state is None and int(chunk.get("updated_at", 0) or 0) < int(time.time()) - 120):
            store.update_chunk(task_id, chunk["chunk_id"], status="failed",
                               error=f"RQ job {state or 'disappeared'}")
    return refresh(store, task_id)
