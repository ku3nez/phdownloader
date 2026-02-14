# Phdownloader

A web-based video downloader powered by Flask and `yt-dlp`. Supports a video hosting services YouTube and PH, with advanced features for quality control and server persistence.

*Provided for informational purposes only, has no commercial interest, all rights belong to their respective owners. The application was created with the help of AI.*

| Base UI version | Sci-Fi UI version |
| :---: | :---: |
| <img src="https://github.com/user-attachments/assets/a2adf751-3585-4a11-8cb1-fa8bf2a6dcfb" width="400"> | <img src="https://github.com/user-attachments/assets/c7dcf90c-53ba-49a6-b447-79828ec3478a" width="400"> |

### 1. Broad Website Support & Formats
*   **Universal Downloader**: Leveraging `yt-dlp` to support hundreds of video platforms.
*   **Audio Extraction**: Ability to download only the audio track in **MP3 (192kbps)** format.
*   **Format Selection**: Toggle between **Video (MP4)** and **Audio (MP3)** with a single click.
*   **Optimization**: When downloading audio, the script automatically uses low-bandwidth streams to save time and server resources.

### 2. Quality & Stability
*   **Resolution Selection**: Choose from various qualities including **360p**, **480p**, **720p**, **1080p**, or **Best Available**.
*   **Concurrent Task Isolation**: Every download is isolated in its own unique task directory, preventing filename collisions and errors during simultaneous downloads.
*   **Enhanced Resilience**: Configured with exponential backoff and increased retry logic (30 retries) to handle network instabilities and `502 Bad Gateway` errors.

### 3. Flexible Download Modes
*   **Standard Mode**: Downloads to a task-specific folder on the server, then triggers a browser download. Files are automatically cleaned up after 5 minutes.
*   **Server-Only Mode**: Permanent storage on the server in the `downloads/` directory, excluded from automatic cleanup.

### 4. Interactive UI
*   **Real-Time Progress**: Track download speed, percentage, size, and ETA.
*   **Live Logs**: Full transparency with technical log streaming directly to the browser.
*   **Localization**: Full support for **English** and **Russian**.

## Installation & Setup

1.  **Clone the repository**:
    ```bash
    git clone <repository-url>
    cd phdownloader
    ```
2.  **External Dependencies**: Ensure `ffmpeg` is installed on your system for audio extraction.
3.  **Install Python dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
4.  **Run the application**:
    ```bash
    python app.py
    ```
    The app will be available at `http://localhost:5008`.

## Deployment (Linux Systemd)

The project includes a `phdownloader.service` template for permanent deployment on Linux servers.

1.  Copy the service file to systemd:
    ```bash
    sudo cp phdownloader.service /etc/systemd/system/
    ```
2.  Reload and start:
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable phdownloader
    sudo systemctl start phdownloader
    ```

## Development

*   **Backend**: Python (Flask, Threading)
*   **Engine**: yt-dlp, FFmpeg
*   **Frontend**: Vanilla JS, HTML5, CSS3
