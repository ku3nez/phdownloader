from flask import Flask, render_template, request, send_file, jsonify
from downloader import download_video
import os
import threading
import uuid
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = 'supersecretkey'

# Global dictionary to store task status
tasks = {}

# Configuration from .env
FILE_EXPIRATION_SECONDS = int(os.getenv('FILE_EXPIRATION_SECONDS', 300))
ENABLE_SAVE_ON_SERVER = os.getenv('ENABLE_SAVE_ON_SERVER', 'False').lower() == 'true'
DEFAULT_VIDEO_QUALITY = os.getenv('DEFAULT_VIDEO_QUALITY', '720')
APP_PORT = int(os.getenv('PORT', 5008))
CLEANUP_INTERVAL_SECONDS = 60  # 1 minute

def cleanup_downloads():
    """Background task to delete old files from the downloads folder."""
    while True:
        try:
            now = time.time()
            download_dir = 'downloads'
            if os.path.exists(download_dir):
                for filename in os.listdir(download_dir):
                    file_path = os.path.join(download_dir, filename)
                    if os.path.isfile(file_path):
                        file_age = now - os.path.getmtime(file_path)
                        if file_age > FILE_EXPIRATION_SECONDS:
                            if filename.startswith('[SERVER]'):
                                continue
                            try:
                                os.remove(file_path)
                                print(f"Deleted old file: {file_path}")
                            except Exception as e:
                                print(f"Error deleting file {file_path}: {e}")
        except Exception as e:
            print(f"Cleanup error: {e}")
        time.sleep(CLEANUP_INTERVAL_SECONDS)

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_downloads, daemon=True)
cleanup_thread.start()

def background_download(task_id, url, quality, server_only=False):
    def update_progress(info):
        if info['type'] == 'progress':
            tasks[task_id]['progress'] = info['percentage']
            tasks[task_id]['details'] = info
        elif info['type'] == 'status':
            tasks[task_id]['logs'].append(info['msg'])
            tasks[task_id]['logs'] = tasks[task_id]['logs'][-20:]  # Keep last 20 lines
            tasks[task_id]['current_status'] = info['msg']
            print(f"[{task_id}] Status: {info['msg']}")

    try:
        filename = download_video(url, output_path='downloads', quality=quality, progress_callback=update_progress)
        if filename and os.path.exists(filename):
            if server_only:
                server_filename = os.path.join(os.path.dirname(filename), '[SERVER] ' + os.path.basename(filename))
                os.rename(filename, server_filename)
                filename = server_filename
            
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['filename'] = filename
            tasks[task_id]['progress'] = 100
            tasks[task_id]['server_only'] = server_only
        else:
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
    server_only = request.json.get('server_only', False)
    
    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "processing", "progress": 0, "filename": None, "error": None, "details": {}, "logs": [], "server_only": server_only}
    
    thread = threading.Thread(target=background_download, args=(task_id, url, quality, server_only))
    thread.start()
    
    return jsonify({"task_id": task_id})

@app.route('/progress/<task_id>')
def get_progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)

@app.route('/get_file/<task_id>')
def get_file(task_id):
    task = tasks.get(task_id)
    if not task or task['status'] != 'completed' or not task['filename']:
        return "File not ready or task not found", 404
    return send_file(task['filename'], as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=APP_PORT)
