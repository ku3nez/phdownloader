"""One line per finished transcription in a shared, append-only log.

Like download_links.log, the file lives on the shared volume, so every node
writes to the same place. Columns are tab-separated (see HEADER).
"""

import os
from datetime import datetime, timezone

from cluster_config import SHARED_STORAGE_ROOT

TRANSCRIPTS_LOG_PATH = os.path.abspath(
    os.getenv("TRANSCRIPTS_LOG_PATH", os.path.join(SHARED_STORAGE_ROOT, "transcripts.log"))
)
HEADER = ("# finished_utc\tstatus\ttask_id\tnodes\taudio\tprocessing\tengine\tlanguage\tspeakers\t"
          "source\tresult\terror\tnote\n")
NODE_ALIASES = {"v398700": "vdsina", "cloud-server-10537970": "onecom", "omega": "ovh"}


def _clean(value) -> str:
    text = " ".join(str(value).split()) if value not in (None, "") else "-"
    return text.replace("\t", " ")


def _duration(seconds) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    if total <= 0:
        return "-"
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _node(name) -> str:
    name = str(name or "").split(".")[0]
    return NODE_ALIASES.get(name, name)


def format_entry(task: dict, chunks: list, status: str, now: float) -> str:
    nodes = []
    for name in [task.get("worker_node")] + [c.get("worker_node") for c in chunks or []]:
        name = _node(name)
        if name and name not in nodes:
            nodes.append(name)
    engine = str(task.get("asr_engine") or "")
    if not engine or engine == "whisper":
        engine = f"whisper-{task.get('model_size') or '?'}"
    language = task.get("asr_language") or task.get("detected_language") or task.get("language") or "-"
    speakers = "-"
    if task.get("diarize"):
        found = task.get("speakers") if isinstance(task.get("speakers"), list) else []
        speakers = str(len(found)) if found else "?"
        if task.get("num_speakers"):
            speakers += f" (given {task['num_speakers']})"
        names = [str(s.get("name")) for s in task.get("speaker_suggestions") or [] if isinstance(s, dict) and s.get("name")]
        if names:
            speakers += ": " + ", ".join(names)
        if task.get("remarks"):
            speakers += " +remarks"
    try:
        processing = now - float(task.get("created_at") or now)
    except (TypeError, ValueError):
        processing = None
    finished = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    columns = [
        finished,
        status,
        task.get("task_id") or "-",
        ",".join(nodes) or "-",
        _duration(task.get("total_duration")),
        _duration(processing),
        engine,
        language,
        speakers,
        task.get("url") or task.get("source_name") or "-",
        os.path.basename(str(task.get("filename") or "")) or "-",
        (task.get("error") if status != "completed" else None) or "-",
        "-",
    ]
    return "\t".join(_clean(c) for c in columns) + "\n"


def append_entry(line: str) -> bool:
    try:
        new = not os.path.exists(TRANSCRIPTS_LOG_PATH)
        with open(TRANSCRIPTS_LOG_PATH, "a", encoding="utf-8") as handle:
            if new:
                handle.write(HEADER)
            handle.write(line)
        return True
    except OSError as exc:
        print(f"Failed to write transcripts log {TRANSCRIPTS_LOG_PATH}: {exc}", flush=True)
        return False
