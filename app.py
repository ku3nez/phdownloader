from flask import Flask, render_template, request, send_file, jsonify
from downloader import download_media
import os
import threading
import uuid
import time
import shutil
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = 'supersecretkey'

# Global dictionary to store task status
tasks = {}

# Configuration from .env
FILE_EXPIRATION_SECONDS = int(os.getenv('FILE_EXPIRATION_SECONDS', 600))
ENABLE_SAVE_ON_SERVER = os.getenv('ENABLE_SAVE_ON_SERVER', 'False').lower() == 'true'
DEFAULT_VIDEO_QUALITY = os.getenv('DEFAULT_VIDEO_QUALITY', '720')
APP_PORT = int(os.getenv('PORT', 5008))
CLEANUP_INTERVAL_SECONDS = 60  # 1 minute

def is_russian_request():
    lang = request.headers.get('Accept-Language', '')
    return 'ru' in lang.lower()

def cleanup_downloads():
    """Background task to delete old files and folders from the downloads folder."""
    while True:
        try:
            now = time.time()
            download_dir = 'downloads'
            if os.path.exists(download_dir):
                for item in os.listdir(download_dir):
                    item_path = os.path.join(download_dir, item)
                    try:
                        # Skip special [SERVER] files or active tasks
                        if item.startswith('[SERVER]'):
                            continue
                        
                        # Protect active tasks from cleanup
                        if item in tasks and tasks[item].get('status') == 'processing':
                            print(f"Cleanup: Skipping active task {item} (in-memory)")
                            continue
                        
                        # Process-independent check: skip directories with an .active file
                        if os.path.isdir(item_path) and os.path.exists(os.path.join(item_path, '.active')):
                            print(f"Cleanup: Skipping active task {item} (file-marker)")
                            continue

                        file_age = now - os.path.getmtime(item_path)
                        if file_age > FILE_EXPIRATION_SECONDS:
                            if os.path.isfile(item_path):
                                os.remove(item_path)
                                print(f"Deleted old file: {item_path}")
                            elif os.path.isdir(item_path):
                                shutil.rmtree(item_path)
                                print(f"Deleted old directory: {item_path}")
                    except Exception as e:
                        print(f"Error deleting {item_path}: {e}")
        except Exception as e:
            print(f"Cleanup error: {e}")
        time.sleep(CLEANUP_INTERVAL_SECONDS)

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_downloads, daemon=True)
cleanup_thread.start()

def background_download(task_id, url, quality, download_type='video', structured=True, model_size='base', server_only=False):
    def update_progress(info):
        if info['type'] == 'progress':
            tasks[task_id]['progress'] = info['percentage']
            tasks[task_id]['details'] = info
        elif info['type'] == 'status':
            raw_msg = info['msg']
            tasks[task_id]['logs'].append(raw_msg)
            tasks[task_id]['logs'] = tasks[task_id]['logs'][-20:]
            
            # Use English headers for mapping but serve localized if we can
            # We detect language from the app context or just provide a key
            # For simplicity, we'll map to friendly Russian/English strings
            friendly_msg = raw_msg
            if "[download]" in raw_msg:
                friendly_msg = "Загрузка медиа..." if is_russian_request() else "Downloading media..."
            elif "[ffmpeg]" in raw_msg or "[ExtractAudio]" in raw_msg or "audio file" in raw_msg:
                friendly_msg = "Подготовка аудио..." if is_russian_request() else "Preparing audio..."
            elif "Whisper" in raw_msg:
                friendly_msg = "Инициализация ИИ..." if is_russian_request() else "Initializing AI..."
            elif "Transcribing" in raw_msg:
                friendly_msg = "Распознавание текста..." if is_russian_request() else "Transcribing text..."
            elif "Transcription complete" in raw_msg:
                friendly_msg = "Завершено ✨" if is_russian_request() else "Complete ✨"

            tasks[task_id]['current_status'] = friendly_msg
            print(f"[{task_id}] Status: {raw_msg} -> {friendly_msg}")

    def update_metadata(info):
        if 'duration' in info:
            duration = info['duration']
            tasks[task_id]['total_duration'] = duration
            print(f"[{task_id}] Total duration: {duration}s")

    def check_cancel():
        cancelled = tasks.get(task_id, {}).get('status') == 'cancelled'
        if cancelled:
            print(f"[{task_id}] Cancellation signal received by background task.")
        return cancelled

    try:
        # Create a task-specific subdirectory to avoid filename collisions
        task_dir = os.path.join('downloads', task_id)
        if not os.path.exists(task_dir):
            os.makedirs(task_dir)

        # Create active marker
        active_marker = os.path.join(task_dir, '.active')
        with open(active_marker, 'w') as f: f.write('active')

        filename = download_media(url, output_path=task_dir, quality=quality, media_type=download_type, structured=structured, model_size=model_size, progress_callback=update_progress, metadata_callback=update_metadata, check_cancel=check_cancel)
        
        # Remove active marker
        if os.path.exists(active_marker): os.remove(active_marker)
        print(f"[{task_id}] download_media returned: {filename}")
        
        if filename and os.path.exists(filename):
            print(f"[{task_id}] File exists at: {filename}")
            if server_only:
                server_filename = os.path.join(os.path.dirname(filename), '[SERVER] ' + os.path.basename(filename))
                os.rename(filename, server_filename)
                filename = server_filename
                print(f"[{task_id}] Renamed for server-only: {filename}")
            
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['filename'] = filename
            tasks[task_id]['progress'] = 100
            tasks[task_id]['server_only'] = server_only
            print(f"[{task_id}] Task completed successfully.")
        else:
            print(f"[{task_id}] File NOT found or filename is None. Exists: {os.path.exists(filename) if filename else 'N/A'}")
            tasks[task_id]['status'] = 'failed'
            if not tasks[task_id].get('error'):
                tasks[task_id]['error'] = 'Download failed'
    except Exception as e:
        tasks[task_id]['status'] = 'failed'
        tasks[task_id]['error'] = str(e)

@app.route('/')
def index():
    return render_template('index.html', 
                           enable_save_on_server=ENABLE_SAVE_ON_SERVER, 
                           default_quality=DEFAULT_VIDEO_QUALITY)

@app.route('/start', methods=['POST'])
def start_download():
    url = request.json.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    quality = request.json.get('quality', DEFAULT_VIDEO_QUALITY)
    download_type = request.json.get('download_type', 'video')
    structured = request.json.get('structured', True)
    model_size = request.json.get('model_size', 'base')
    server_only = request.json.get('server_only', False)

    if download_type == 'transcript':
        # Check for other active transcriptions
        active_trans = [tid for tid, t in tasks.items() if t.get('status') == 'processing' and t.get('download_type') == 'transcript']
        if active_trans:
            other_tid = active_trans[0]
            eta_min = calculate_eta(other_tid)
            
            if eta_min:
                msg_ru = f"Сервер занят другой транскрипцией. Пожалуйста, подождите примерно {eta_min} мин."
                msg_en = f"Server is busy with another transcription. Please wait approximately {eta_min} min."
            else:
                msg_ru = "Сервер занят другой транскрипцией. Пожалуйста, попробуйте через пару минут."
                msg_en = "Server is busy with another transcription. Please try again in a few minutes."
            
            return jsonify({"error": msg_ru if request.headers.get('Accept-Language', '').startswith('ru') else msg_en, "busy": True}), 429
    
    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "processing", 
        "progress": 0, 
        "filename": None, 
        "error": None, 
        "details": {}, 
        "logs": [], 
        "server_only": server_only,
        "structured": structured,
        "model_size": model_size,
        "download_type": download_type
    }
    
    thread = threading.Thread(target=background_download, args=(task_id, url, quality, download_type, structured, model_size, server_only))
    thread.start()
    
    return jsonify({"task_id": task_id})

def calculate_eta(task_id):
    task = tasks.get(task_id)
    if not task or task.get('status') != 'processing':
        return None
    
    total_duration = task.get('total_duration', 0)
    if total_duration == 0:
        return None
        
    progress = task.get('progress', 0)
    # We only care about remaining time for transcription
    if task.get('download_type') != 'transcript':
        return None
        
    # Only show ETA if we have some progress but hasn't reached 100%
    if progress <= 0 or progress >= 100:
        return None
        
    remaining_video_sec = total_duration * (1 - progress/100)
    factor = 0.6 if task.get('model_size') == 'small' else 0.25
    return max(1, int(remaining_video_sec * factor / 60))

@app.route('/progress/<task_id>')
def get_progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
        
    # Inject real-time ETA
    data = task.copy()
    eta = calculate_eta(task_id)
    if eta is not None:
        data['eta_minutes'] = eta
        
    return jsonify(data)

@app.route('/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    if task_id in tasks:
        tasks[task_id]['status'] = 'cancelled'
        tasks[task_id]['error'] = 'Task cancelled by user'
        return jsonify({"success": True})
    return jsonify({"error": "Task not found"}), 404

@app.route('/get_file/<task_id>')
def get_file(task_id):
    task = tasks.get(task_id)
    if not task or task['status'] != 'completed' or not task['filename']:
        return "File not ready or task not found", 404
    return send_file(task['filename'], as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=APP_PORT)
