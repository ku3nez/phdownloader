import os
from dotenv import load_dotenv

load_dotenv()

APP_PORT = int(os.getenv("PORT", 5008))
DEFAULT_VIDEO_QUALITY = os.getenv("DEFAULT_VIDEO_QUALITY", "720")
ENABLE_SAVE_ON_SERVER = os.getenv("ENABLE_SAVE_ON_SERVER", "False").lower() == "true"
FILE_EXPIRATION_SECONDS = int(os.getenv("FILE_EXPIRATION_SECONDS", 600))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", 60))
TASK_STALL_TIMEOUT_SECONDS = int(os.getenv("TASK_STALL_TIMEOUT_SECONDS", 300))
# A job that is still waiting in an RQ queue is not stalled; it only fails
# when no worker has picked it up within this longer period.
TASK_QUEUE_TIMEOUT_SECONDS = int(os.getenv("TASK_QUEUE_TIMEOUT_SECONDS", 60 * 60 * 3))
# Running jobs refresh their task this often, so long silent steps such as
# FFmpeg merges or audio splitting are not mistaken for dead workers.
TASK_HEARTBEAT_SECONDS = max(5, int(os.getenv("TASK_HEARTBEAT_SECONDS", min(60, max(5, TASK_STALL_TIMEOUT_SECONDS // 4)))))
TRANSCRIPTION_CHUNK_RETRIES = max(0, int(os.getenv("TRANSCRIPTION_CHUNK_RETRIES", 2)))
TRANSCRIPTION_CHUNK_RETRY_DELAY_SECONDS = max(0, int(os.getenv("TRANSCRIPTION_CHUNK_RETRY_DELAY_SECONDS", 10)))

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/7")
RQ_DEFAULT_QUEUE_NAME = os.getenv("RQ_DEFAULT_QUEUE_NAME", "phdownloader-default")
RQ_TRANSCRIPT_QUEUE_NAME = os.getenv("RQ_TRANSCRIPT_QUEUE_NAME", "phdownloader-transcript")
RQ_PORNHUB_QUEUE_NAME = os.getenv("RQ_PORNHUB_QUEUE_NAME", "phdownloader-pornhub")
RQ_TELEGRAM_QUEUE_NAME = os.getenv("RQ_TELEGRAM_QUEUE_NAME", "phdownloader-telegram")
RQ_JOB_TIMEOUT = int(os.getenv("RQ_JOB_TIMEOUT", 60 * 60 * 6))
RQ_RESULT_TTL = int(os.getenv("RQ_RESULT_TTL", 60 * 60 * 24))
TRANSCRIPTION_CHUNK_SECONDS = int(os.getenv("TRANSCRIPTION_CHUNK_SECONDS", 600))
TRANSCRIPTION_MIN_DISTRIBUTED_SECONDS = int(os.getenv("TRANSCRIPTION_MIN_DISTRIBUTED_SECONDS", 900))
TRANSCRIPTION_DISTRIBUTED_ENABLED = os.getenv("TRANSCRIPTION_DISTRIBUTED_ENABLED", "true").lower() == "true"
TASK_LOCK_TTL_SECONDS = int(os.getenv("TASK_LOCK_TTL_SECONDS", 60 * 10))

SHARED_STORAGE_ROOT = os.path.abspath(os.getenv("SHARED_STORAGE_ROOT", "downloads"))
TASKS_ROOT = os.path.join(SHARED_STORAGE_ROOT, "tasks")
