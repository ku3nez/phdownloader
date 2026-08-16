# Phdownloader

A web application for downloading media and creating transcripts. Flask accepts requests, Redis/RQ runs them in isolated workers, and `yt-dlp`, FFmpeg, and Faster Whisper perform media processing.

*Provided for informational purposes only, has no commercial interest, all rights belong to their respective owners. The application was created with the help of AI.*

| Base UI version | Sci-Fi UI version |
| :---: | :---: |
| <img src="https://github.com/user-attachments/assets/a2adf751-3585-4a11-8cb1-fa8bf2a6dcfb" width="400"> | <img src="https://github.com/user-attachments/assets/c7dcf90c-53ba-49a6-b447-79828ec3478a" width="400"> |

## Features

### Media downloads

- **Multi-site extraction:** uses `yt-dlp` and supports YouTube, PornHub, and the other sites supported by that extractor.
- **Video and audio modes:** downloads MP4 video or extracts MP3 audio. Video quality can be 360p, 480p, 720p, 1080p, or the best available format.
- **Reliable format selection:** prefers separate MP4 video and M4A audio streams, then uses compatible fallbacks when a selected quality is unavailable.
- **Browser impersonation:** installs `curl-cffi` and requests a Chrome impersonation target for hosts that require a browser-like TLS fingerprint.
- **Retry handling:** retries network and fragmented downloads; transient YouTube player-response failures are retried with backoff.
- **Useful failures:** extractor errors are returned as the task error instead of being replaced with a generic missing-output message.

### Transcription

- **Audio or video transcription:** accepts a remote media URL or an uploaded media file.
- **Faster Whisper processing:** supports selectable model sizes, CPU thread configuration, voice-activity detection, timestamps, and structured or continuous-text output.
- **Distributed mode for long files:** media longer than `TRANSCRIPTION_MIN_DISTRIBUTED_SECONDS` is split into FFmpeg chunks. Each chunk is queued independently and its timestamps are merged into one final transcript.
- **Progress and ETA:** reports per-download and per-transcription progress; distributed jobs aggregate chunk progress.

### Task execution and storage

- **Redis/RQ queues:** the API puts work into separate default-media and transcription queues. RQ workers can run on multiple nodes.
- **Task isolation:** every task uses its own `${SHARED_STORAGE_ROOT}/tasks/<task-id>` directory, preventing collisions between concurrent requests.
- **Cancellation and cleanup:** queued/running tasks can be cancelled. Expired completed task directories are removed automatically; active tasks are protected by an `.active` marker.
- **Shared storage:** API and workers use `SHARED_STORAGE_ROOT`; it must point to the same mounted directory on every node that participates in a cluster.
- **Source-link audit log:** each successful remote media download is appended to `${SHARED_STORAGE_ROOT}/download_links.log`, independent of which worker processed it.

### Web interface

- Real-time progress, size, speed, ETA, task status, and technical log output.
- Copy-to-clipboard action for the task log.
- English and Russian interface text.
- Normal browser download or server-only retention mode.

## Architecture

```text
Browser -> Flask API -> Redis/RQ queues -> one or more RQ workers
                              |                    |
                              +--------------------+
                                       shared storage
                            ${SHARED_STORAGE_ROOT}/tasks
                            ${SHARED_STORAGE_ROOT}/download_links.log
```

## Installation & Setup

1.  **Clone the repository**:
    ```bash
    git clone <repository-url>
    cd phdownloader
    ```
2.  **External dependencies**: use Python **3.11 or newer**, FFmpeg, and aria2. The deployment script installs them. `aria2` is used for multi-connection media downloads; the dependency set installs `curl-cffi`, used by `yt-dlp` to impersonate a browser when a video host requires it.
3.  **Install Python dependencies**:
    ```bash
    python3.11 -m pip install -r requirements.txt
    ```
4.  **Run the application**:
    ```bash
    python3.11 app.py
    ```
    The app will be available at `http://localhost:5008`. For remote jobs, a Redis server and at least one worker are also required.

5. **Start an RQ worker** in a second terminal:

    ```bash
    rq worker phdownloader-default phdownloader-transcript --url "$REDIS_URL"
    ```

## Configuration

Set configuration values in `.env`. Do not commit that file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `REDIS_URL` | `redis://127.0.0.1:6379/7` | Redis connection used for tasks and queues. |
| `SHARED_STORAGE_ROOT` | `downloads` | Shared directory mounted identically on the API and every worker node. |
| `RQ_DEFAULT_QUEUE_NAME` | `phdownloader-default` | Queue for video and audio downloads. |
| `RQ_TRANSCRIPT_QUEUE_NAME` | `phdownloader-transcript` | Queue for transcription and distributed chunks. |
| `RQ_WORKER_PROCESSES` | `1` | Worker processes started by the systemd worker unit. |
| `RQ_WORKER_QUEUES` | `phdownloader-default` | Queues consumed by the systemd worker unit. Include the transcription queue when this node handles transcription. |
| `TRANSCRIPTION_DISTRIBUTED_ENABLED` | `true` | Enables splitting long media into distributed transcription chunks. |
| `TRANSCRIPTION_MIN_DISTRIBUTED_SECONDS` | `900` | Minimum media duration that activates distributed transcription. |
| `TRANSCRIPTION_CHUNK_SECONDS` | `600` | Target duration of each FFmpeg audio chunk. |
| `DOWNLOAD_LINKS_LOG_PATH` | `${SHARED_STORAGE_ROOT}/download_links.log` | Optional explicit path for the successful-download source-link log. |
| `YT_DLP_COOKIE_FILE` | `cookies.txt` | Optional Netscape cookie file for sites that require an authenticated session. |
| `YT_DLP_COOKIES_BROWSER` | unset | Optional browser name used to read local cookies on the worker. |
| `YT_DLP_PROXY` | unset | Optional HTTP/SOCKS proxy URL for yt-dlp. |
| `YT_DLP_JS_RUNTIME` | `node` | JavaScript runtime passed to yt-dlp. |

## Deployment (Linux Systemd)

The application requires separate API and RQ worker services. On each server, deploy the same Git commit, configure `/opt/phdownloader/.env`, and ensure `SHARED_STORAGE_ROOT` references the same mounted storage on every node.

```bash
cd /opt/phdownloader
sudo ./deploy/setup.sh
```

The script requires and creates a Python 3.11+ virtual environment, installs `curl-cffi`, and starts `phdownloader-api` and `phdownloader-worker`. If an existing virtual environment uses Python 3.10, it is preserved with a timestamped `.python310.*` suffix and replaced. It disables the obsolete single-process `phdownloader` service so it cannot occupy the API port.

Downloaded-media links are logged in `${SHARED_STORAGE_ROOT}/download_links.log` by default. Set `DOWNLOAD_LINKS_LOG_PATH` in `.env` only when a different shared path is required.

## Development

* **Backend:** Python, Flask, Redis, RQ
* **Media engine:** yt-dlp, curl-cffi, FFmpeg, Faster Whisper
* **Frontend:** Vanilla JS, HTML5, CSS3
