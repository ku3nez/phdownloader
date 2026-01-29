# Phdownloader

A web-based video downloader powered by Flask and `yt-dlp`. Supports a video hosting services YouTube and PH, with advanced features for quality control and server persistence.

*Provided for informational purposes only, has no commercial interest, all rights belong to their respective owners. The application was created with the help of AI.*

| Base UI version | Sci-Fi UI version |
| :---: | :---: |
| <img src="https://github.com/user-attachments/assets/a2adf751-3585-4a11-8cb1-fa8bf2a6dcfb" width="400"> | <img src="https://github.com/user-attachments/assets/c7dcf90c-53ba-49a6-b447-79828ec3478a" width="400"> |

## Features

### 1. Broad Website Support
*   **Universal Downloader**: Leveraging `yt-dlp` to support hundreds of video platforms.
*   **YouTube Optimization**: Specifically configured to bypass modern streaming restrictions (SABR/403 Forbidden errors) using optimized client arguments.

### 2. Quality Control
*   **Resolution Selection**: Choose from various qualities including **360p**, **480p**, **720p**, **1080p**, or **Best Available**.
*   **Smart Naming**: Downloaded files are automatically named with their quality suffix (e.g., `VideoTitle_1080p.mp4`) for easy identification.

### 3. Flexible Download Modes
*   **Standard Mode**: Downloads the video to the server first, then automatically triggers a download in your browser. Standard downloads are automatically cleaned up after 5 minutes to save storage.
*   **Server-Only Mode**: Save files directly to the server's `downloads/` directory. These files are marked with a `[SERVER]` prefix and are **excluded from automatic deletion**, making them permanent until manual removal.

### 4. Interactive UI
*   **Real-Time Progress**: Track download speed, percentage, total size, and estimated time remaining.
*   **Live Logs**: View detailed technical logs directly in the browser during the extraction and download process.
*   **Localization**: Automated UI translation between **English** and **Russian** based on your browser's language settings.

## Installation & Setup

1.  **Clone the repository**:
    ```bash
    git clone <repository-url>
    cd phdownloader
    ```
2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Run the application**:
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
*   **Engine**: yt-dlp
*   **Frontend**: Vanilla JS, HTML5, CSS3
