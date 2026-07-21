import json
import time
from typing import Any

from redis import Redis
from rq import Queue
from rq.job import Job

from cluster_config import REDIS_URL, RQ_DEFAULT_QUEUE_NAME, RQ_JOB_TIMEOUT, RQ_RESULT_TTL, TASK_LOCK_TTL_SECONDS


def utc_ts() -> int:
    return int(time.time())


class TaskStore:
    def __init__(self):
        self.redis = Redis.from_url(REDIS_URL, decode_responses=True)
        self.rq_redis = Redis.from_url(REDIS_URL, decode_responses=False)
        self.default_queue_name = RQ_DEFAULT_QUEUE_NAME

    def task_key(self, task_id: str) -> str:
        return f"phd:task:{task_id}"

    def logs_key(self, task_id: str) -> str:
        return f"phd:task:{task_id}:logs"

    def chunks_key(self, task_id: str) -> str:
        return f"phd:task:{task_id}:chunks"

    def lock_key(self, task_id: str, name: str) -> str:
        return f"phd:task:{task_id}:lock:{name}"

    def save_task(self, task_id: str, data: dict[str, Any]) -> None:
        now = utc_ts()
        payload = {k: self._encode(v) for k, v in data.items()}
        payload["created_at"] = str(now)
        payload["updated_at"] = str(now)
        self.redis.hset(self.task_key(task_id), mapping=payload)
        self.redis.delete(self.logs_key(task_id))

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        payload = {k: self._encode(v) for k, v in fields.items()}
        payload["updated_at"] = str(utc_ts())
        self.redis.hset(self.task_key(task_id), mapping=payload)

    def append_log(self, task_id: str, message: str, limit: int = 200) -> None:
        key = self.logs_key(task_id)
        self.redis.rpush(key, message)
        self.redis.ltrim(key, -limit, -1)
        self.update_task(task_id, last_log=message)

    def get_logs(self, task_id: str) -> list[str]:
        return self.redis.lrange(self.logs_key(task_id), 0, -1)

    def save_chunks(self, task_id: str, chunks: list[dict[str, Any]]) -> None:
        key = self.chunks_key(task_id)
        pipe = self.redis.pipeline()
        pipe.delete(key)
        if chunks:
            payload = {str(chunk["chunk_id"]): json.dumps(chunk, ensure_ascii=False) for chunk in chunks}
            pipe.hset(key, mapping=payload)
        pipe.execute()

    def get_chunks(self, task_id: str) -> list[dict[str, Any]]:
        raw = self.redis.hgetall(self.chunks_key(task_id))
        if not raw:
            return []
        chunks = []
        for payload in raw.values():
            try:
                chunks.append(json.loads(payload))
            except Exception:
                continue
        return sorted(chunks, key=lambda item: int(item.get("index", 0)))

    def get_chunk(self, task_id: str, chunk_id: str) -> dict[str, Any] | None:
        raw = self.redis.hget(self.chunks_key(task_id), chunk_id)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def update_chunk(self, task_id: str, chunk_id: str, **fields: Any) -> dict[str, Any] | None:
        chunk = self.get_chunk(task_id, chunk_id)
        if not chunk:
            return None
        chunk.update(fields)
        chunk["updated_at"] = utc_ts()
        self.redis.hset(self.chunks_key(task_id), chunk_id, json.dumps(chunk, ensure_ascii=False))
        return chunk

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        raw = self.redis.hgetall(self.task_key(task_id))
        if not raw:
            return None
        task = {k: self._decode(v) for k, v in raw.items()}
        task["logs"] = self.get_logs(task_id)
        task["chunks"] = self.get_chunks(task_id)
        return task

    def set_progress(self, task_id: str, percentage: float, details: dict[str, Any] | None = None) -> None:
        self.update_task(task_id, progress=round(float(percentage), 2), details=details or {})

    def set_status(self, task_id: str, status: str, **fields: Any) -> None:
        self.update_task(task_id, status=status, **fields)

    def cancel_task(self, task_id: str) -> bool:
        if not self.redis.exists(self.task_key(task_id)):
            return False
        self.update_task(task_id, status="cancelled", error="Task cancelled by user", cancel_requested=True)
        return True

    def is_cancelled(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        return bool(task and (task.get("status") == "cancelled" or task.get("cancel_requested")))

    def delete_task(self, task_id: str) -> None:
        self.redis.delete(self.task_key(task_id))
        self.redis.delete(self.logs_key(task_id))
        self.redis.delete(self.chunks_key(task_id))

    def summarize_chunks(self, task_id: str) -> dict[str, Any]:
        chunks = self.get_chunks(task_id)
        if not chunks:
            return {
                "count": 0,
                "completed": 0,
                "failed": 0,
                "processing": 0,
                "queued": 0,
                "progress": 0.0,
            }
        total_duration = sum(float(chunk.get("duration", 0) or 0) for chunk in chunks)
        weighted_progress = 0.0
        completed = failed = processing = queued = 0
        for chunk in chunks:
            status = chunk.get("status", "queued")
            duration = float(chunk.get("duration", 0) or 0)
            progress = float(chunk.get("progress", 0) or 0)
            weighted_progress += duration * progress
            if status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
            elif status == "processing":
                processing += 1
            else:
                queued += 1
        overall_progress = 0.0
        if total_duration > 0:
            overall_progress = round(weighted_progress / total_duration, 2)
        return {
            "count": len(chunks),
            "completed": completed,
            "failed": failed,
            "processing": processing,
            "queued": queued,
            "progress": overall_progress,
        }

    def acquire_lock(self, task_id: str, name: str, ttl: int = TASK_LOCK_TTL_SECONDS) -> bool:
        return bool(self.redis.set(self.lock_key(task_id, name), str(utc_ts()), nx=True, ex=ttl))

    def release_lock(self, task_id: str, name: str) -> None:
        self.redis.delete(self.lock_key(task_id, name))

    def enqueue(self, func: str, *args: Any, queue_name: str | None = None, **kwargs: Any):
        queue = Queue(
            queue_name or self.default_queue_name,
            connection=self.rq_redis,
            default_timeout=RQ_JOB_TIMEOUT,
            result_ttl=RQ_RESULT_TTL,
        )
        return queue.enqueue(func, *args, **kwargs)

    def get_job_status(self, job_id: str | None) -> str | None:
        if not job_id:
            return None
        try:
            job = Job.fetch(job_id, connection=self.rq_redis)
        except Exception:
            return None
        try:
            return job.get_status(refresh=True)
        except Exception:
            return None

    @staticmethod
    def _encode(value: Any) -> str:
        if isinstance(value, (dict, list, tuple, bool)) or value is None:
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def _decode(value: str) -> Any:
        if value == "":
            return value
        try:
            return json.loads(value)
        except Exception:
            pass
        if value.isdigit():
            return int(value)
        try:
            return float(value)
        except ValueError:
            return value
