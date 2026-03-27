import yt_dlp
import sys
import os
import traceback
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except ImportError:
    ImpersonateTarget = None

def download_media(url, output_path='downloads', quality='720', media_type='video', progress_callback=None):
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    import re
    def clean_vtt(vtt_path):
        """Simple cleaner for VTT files to get plain text."""
        try:
            with open(vtt_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            text_lines = []
            for line in lines:
                line = line.strip()
                # Skip VTT headers, timestamps, and common metadata
                if not line or line.startswith('WEBVTT') or line.startswith('Kind:') or line.startswith('Language:') or '-->' in line or line.isdigit():
                    continue
                # Remove HTML-like tags (e.g., <c>)
                line = re.sub(r'<[^>]+>', '', line)
                # Avoid exact duplicate consecutive lines (common in VTT)
                if not text_lines or text_lines[-1] != line:
                    text_lines.append(line)
            
            return ' '.join(text_lines)
        except Exception as e:
            return f"Error cleaning VTT: {e}"

    def transcribe_with_whisper(audio_path, output_path):
        """Transcribe audio file using Whisper AI."""
        if progress_callback:
            progress_callback({'type': 'status', 'msg': "Initializing Whisper AI (this may take a moment)..."})
        
        from faster_whisper import WhisperModel
        from tqdm import tqdm
        
        # Using 'base' model for a good balance of speed and accuracy on CPU
        model = WhisperModel("base", device="cpu", compute_type="int8")
        
        if progress_callback:
            progress_callback({'type': 'status', 'msg': "Transcribing audio..."})
            
        segments, info = model.transcribe(audio_path, beam_size=5)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            # Note: segments is a generator, so we iterate through it
            for segment in segments:
                f.write(segment.text + " ")
        
        if progress_callback:
            progress_callback({'type': 'status', 'msg': "Transcription complete."})
        
        return output_path

    def strip_ansi(text):
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    class YdlLogger:
        def debug(self, msg):
            if msg.startswith('[download]') and '%' in msg:
                return
            if progress_callback:
                progress_callback({'type': 'status', 'msg': strip_ansi(msg)})
        def warning(self, msg):
            if progress_callback:
                progress_callback({'type': 'status', 'msg': f"WARNING: {strip_ansi(msg)}"})
        def error(self, msg):
            print(f"yt-dlp ERROR: {msg}")
            if progress_callback:
                progress_callback({'type': 'status', 'msg': f"ERROR: {strip_ansi(msg)}"})

    def hook(d):
        if d['status'] == 'downloading':
            if progress_callback:
                # Extract details and strip ANSI codes
                p_str = strip_ansi(d.get('_percent_str', '0%')).replace('%', '').strip()
                try:
                    percentage = float(p_str)
                except ValueError:
                    percentage = 0.0

                progress_info = {
                    'type': 'progress',
                    'percentage': percentage,
                    'speed': strip_ansi(d.get('_speed_str', 'N/A')),
                    'downloaded': strip_ansi(d.get('_downloaded_bytes_str', 'N/A')),
                    'total': strip_ansi(d.get('_total_bytes_str', d.get('_total_bytes_estimate_str', 'N/A'))),
                    'eta': strip_ansi(d.get('_eta_str', 'N/A'))
                }
                progress_callback(progress_info)

    cookies_browser = os.getenv('YT_DLP_COOKIES_BROWSER')
    js_runtime = os.getenv('YT_DLP_JS_RUNTIME')
    cookie_file = os.getenv('YT_DLP_COOKIE_FILE')
    ydl_opts = {
        'noplaylist': True,
        'quiet': False,
        'logger': YdlLogger(),
        'concurrent_fragment_downloads': 4,
        'retries': 30,
        'fragment_retries': 30,
        'retry_sleep_functions': {'http': lambda n: 5 * 2 ** n}, # Exponential backoff for HTTP errors
        'socket_timeout': 60,
        'nocontinue': False,
        'hls_prefer_native': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'log_t_steps': True,
        'impersonate': ImpersonateTarget.from_str('chrome') if ImpersonateTarget else 'chrome',
        'http_headers': {
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.pornhub.com/',
        },
        'progress_hooks': [hook],
        'cookiesfrombrowser': (cookies_browser,) if cookies_browser and cookies_browser.strip() and not (cookie_file and os.path.exists(cookie_file)) else None,
        'cookiefile': cookie_file if cookie_file and os.path.exists(cookie_file) else None,
        'js_runtimes': {js_runtime: {}} if js_runtime else None,
        'remote_components': ['ejs:github'],
    }

    if media_type == 'audio':
        # Prefer standalone audio, if not available, grab the lowest resolution video to save bandwidth
        ydl_opts['format'] = 'bestaudio/best[height<=360]/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        ydl_opts['outtmpl'] = os.path.join(output_path, '%(title)s_audio.%(ext)s')
    else:
        # Format mapping for better control
        if quality == 'best':
            format_str = 'bestvideo+bestaudio/best'
        else:
            # Try to get the specific resolution or the next best thing below it
            format_str = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]'

        quality_suffix = f"_{quality}p" if quality != 'best' else "_best"
        ydl_opts['format'] = format_str
        ydl_opts['outtmpl'] = os.path.join(output_path, f'%(title)s{quality_suffix}.%(ext)s')
        ydl_opts['merge_output_format'] = 'mp4'

    if media_type == 'transcript':
        # Prepare for subtitle extraction
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsubtitles'] = True
        ydl_opts['subtitleslangs'] = ['ru', 'en']
        ydl_opts['skip_download'] = True # We'll start by trying to just get subtitles
        ydl_opts['outtmpl'] = os.path.join(output_path, '%(title)s.%(ext)s')

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            if media_type == 'transcript':
                # Check if subtitles were downloaded
                subtitle_files = []
                base_name, _ = os.path.splitext(filename)
                
                # Look for downloaded subtitle files (.vtt)
                for ext in ['.ru.vtt', '.en.vtt', '.ru.vtt', '.en.vtt']: # yt-dlp might add lang codes
                    for f in os.listdir(output_path):
                        if f.startswith(os.path.basename(base_name)) and f.endswith('.vtt'):
                            subtitle_files.append(os.path.join(output_path, f))
                
                if subtitle_files:
                    if progress_callback:
                        progress_callback({'type': 'status', 'msg': "Subtitles found, extracting text..."})
                    
                    transcript_path = base_name + "_transcript.txt"
                    # Use the first one found
                    clean_text = clean_vtt(subtitle_files[0])
                    with open(transcript_path, 'w', encoding='utf-8') as f:
                        f.write(clean_text)
                    
                    # Cleanup VTT files
                    for f in subtitle_files:
                        try: os.remove(f)
                        except: pass
                    
                    return transcript_path
                else:
                    if progress_callback:
                        progress_callback({'type': 'status', 'msg': "No subtitles found. Falling back to Whisper AI..."})
                    
                    # Need to download audio now
                    audio_ydl_opts = ydl_opts.copy()
                    audio_ydl_opts['skip_download'] = False
                    audio_ydl_opts['format'] = 'bestaudio/best'
                    audio_ydl_opts['postprocessors'] = [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '128',
                    }]
                    
                    with yt_dlp.YoutubeDL(audio_ydl_opts) as audio_ydl:
                        audio_info = audio_ydl.extract_info(url, download=True)
                        audio_file = audio_ydl.prepare_filename(audio_info)
                        # Adjustment for audio extension
                        base, _ = os.path.splitext(audio_file)
                        if os.path.exists(base + '.mp3'):
                            audio_file = base + '.mp3'
                        
                        transcript_path = base + "_transcript.txt"
                        transcribe_with_whisper(audio_file, transcript_path)
                        
                        # Cleanup audio file after transcription
                        try: os.remove(audio_file)
                        except: pass
                        
                        return transcript_path

            if media_type == 'audio':
                # Extension will be .mp3 after post-processing
                base, _ = os.path.splitext(filename)
                if os.path.exists(base + '.mp3'):
                    filename = base + '.mp3'
            else:
                # If it was merged, the extension might have changed to mp4
                if not os.path.exists(filename):
                    base, _ = os.path.splitext(filename)
                    if os.path.exists(base + '.mp4'):
                        filename = base + '.mp4'
            return filename
    except Exception as e:
        # General cleanup try for any partial files
        pass
        
        error_msg = f"{str(e)}"
        if not error_msg.strip():
            error_msg = f"Unknown Error: {type(e).__name__}"
        
        full_tb = traceback.format_exc()
        print(f"DOWNLOAD EXCEPTION CAUGHT:\n{full_tb}")
        
        if progress_callback:
            progress_callback({'type': 'status', 'msg': f"ERROR: {error_msg}"})
            progress_callback({'type': 'status', 'msg': "Check server console for full traceback."})
        return None

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python downloader.py <URL> [type: video|audio|transcript]")
        # Default for testing if no URL provided
        video_url = "https://www.youtube.com/watch?v=w2OC_0P3HJk"
        media_type = "video"
        print(f"Using default test URL: {video_url}")
    else:
        video_url = sys.argv[1]
        media_type = sys.argv[2] if len(sys.argv) > 2 else "video"
    
    download_media(video_url, media_type=media_type)
