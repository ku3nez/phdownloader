"""What the page may see of a task.

The task hash and its log hold internal details (file paths on the shared
volume, server host names and addresses, queue and job ids, client IPs,
Python tracebacks). /progress returns only the fields the page uses, and log
lines are rewritten or dropped so they stay readable without exposing them.
"""

import os
import re

TASK_FIELDS = {
    "status", "progress", "details", "current_status", "error", "download_type", "eta_minutes",
    "publish_to_telegram", "telegram_status", "telegram_error", "speakers", "speaker_suggestions",
    "speaker_names", "speaker_names_status", "server_only", "processing_mode", "playlist_title",
    "chunk_summary", "total_duration", "worker_node",
}
CHUNK_FIELDS = {
    "chunk_id", "index", "status", "progress", "offset", "duration", "title", "worker_node", "details",
    "error", "last_status",
}
NODE_ALIASES = {
    "cloud-server-10537970": "onecom", "v398700": "vdsina", "v398700.hosted-by-vdsina.com": "vdsina",
    "omega": "ovh",
}

TASK_PREFIX = re.compile(r"^\[[0-9a-f]{8}-[0-9a-f-]{27}\]\s*")
DROP = re.compile(
    r"^(Task created on API node|Enqueued rq_job_id|enqueued chunk-|Traceback|File \"|Check server console|"
    r"split_audio|deleted source file|task switched to distributed|remote transcript switched|"
    r"\[cleanup\]|heartbeat|.*Writing temporary cookies file|.*cookie file is unavailable|"
    r"Saving uploaded file|Uploaded file saved|GET /|POST /|single_transcription|distributed_transcription|"
    r"process_transcription_chunk|chunk attempt failed|skip finalize|Cancellation signal received)",
    re.I,
)
INTERNAL_KEYS = re.compile(
    r"\s*\b(task_dir|rq_job_id|queue|queue_name|remote_addr|chunks_dir|transcript_path|embeddings_path|job_id)=\S+"
)
PATH = re.compile(r"(?<![\w:/])/(?:mnt|opt|root|tmp|var|home|usr|srv|etc|proc|sys|dev|run)/[^\s'\",)\]]*")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")
HOSTS = re.compile("|".join(re.escape(h) for h in sorted(NODE_ALIASES, key=len, reverse=True)))


def node_name(name):
    name = str(name or "")
    return NODE_ALIASES.get(name, NODE_ALIASES.get(name.split(".")[0], name)) or None


def clean_text(text, russian=False):
    """One message for the page, or None when it should not be shown."""
    if text is None:
        return None
    text = TASK_PREFIX.sub("", str(text)).strip()
    if not text or text.startswith((" ", "\t")) or DROP.match(text):
        return None
    match = re.match(r"(process_remote_media|process_uploaded_transcription|process_uploaded_video) worker_node=(\S+)", text)
    if match:
        server = node_name(match.group(2))
        return f"Задачу взял сервер {server}" if russian else f"Picked up by server {server}"
    match = re.match(r"playlist item (\d+) worker_node=(\S+)", text)
    if match:
        server = node_name(match.group(2))
        return f"Видео {match.group(1)}: сервер {server}" if russian else f"Video {match.group(1)}: server {server}"
    text = re.sub(r"\b(?:ERROR: )+", "ERROR: ", text)
    text = INTERNAL_KEYS.sub("", text)
    text = PATH.sub(lambda m: os.path.basename(m.group(0).rstrip("/")) or "…", text)
    text = HOSTS.sub(lambda m: NODE_ALIASES[m.group(0)], text)
    text = re.sub(r"worker_node=(\S+)", lambda m: f"server={node_name(m.group(1))}", text)
    text = IPV4.sub("…", text)
    return text.strip() or None


def public_task(task, task_id):
    russian = bool(task.get("is_russian"))
    view = {key: task.get(key) for key in TASK_FIELDS if key in task}
    view["task_id"] = task_id
    view["worker_node"] = node_name(task.get("worker_node"))
    for key in ("current_status", "error", "telegram_error"):
        if view.get(key):
            lines = [clean_text(line, russian) for line in str(view[key]).splitlines()]
            lines = [line for line in lines if line]
            view[key] = lines[-1] if lines else None
    logs, previous = [], None
    for line in task.get("logs") or []:
        line = clean_text(line, russian)
        if line and line != previous:
            logs.append(line)
        previous = line or previous
    view["logs"] = logs
    chunks = []
    for chunk in task.get("chunks") or []:
        item = {key: chunk.get(key) for key in CHUNK_FIELDS if key in chunk}
        item["worker_node"] = node_name(chunk.get("worker_node"))
        for key in ("error", "last_status"):
            if item.get(key):
                item[key] = clean_text(item[key], russian)
        chunks.append(item)
    view["chunks"] = chunks
    return view
