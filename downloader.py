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

def download_media(url, output_path='downloads', quality='720', media_type='video', structured=True, model_size='base', progress_callback=None, metadata_callback=None, check_cancel=None):
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    import re
    def clean_vtt(vtt_path, structured=True):
        """Simple parser to convert VTT to clean text with optional timestamps."""
        import re
        clean_lines = []
        last_timestamp = ""
        
        try:
            with open(vtt_path, 'r', encoding='utf-8') as f:
                for line in f:
                    # Look for timestamp line: 00:00:05.120 --> 00:00:10.000
                    ts_match = re.search(r'(\d{2}):(\d{2}):(\d{2})\.\d{3} -->', line)
                    if ts_match:
                        h, m, s = ts_match.groups()
                        if int(h) > 0:
                            last_timestamp = f"[{h}:{m}:{s}] "
                        else:
                            last_timestamp = f"[{m}:{s}] "
                        continue

                    line = line.strip()
                    # Skip numeric lines and VTT headers
                    if not line or line.isdigit() or line.upper() == 'WEBVTT' or line.startswith('NOTE'):
                        continue
                    
                    # If we have a timestamp for this line, use it
                    if structured and last_timestamp:
                        clean_lines.append(f"{last_timestamp}{line}")
                        last_timestamp = "" # Use it once per block
                        # Add paragraph break if line ends with sentence terminator
                        if line.endswith(('.', '!', '?')):
                            clean_lines.append("")
                    elif not structured:
                        # Just append text
                        if clean_lines and not clean_lines[-1].endswith(' '):
                            clean_lines.append(" " + line)
                        else:
                            clean_lines.append(line)
                    else:
                        # Structured but no timestamp available for this line yet
                        clean_lines.append(line)

            return ("\n" if structured else " ").join(clean_lines)
        except Exception as e:
            return f"Error cleaning VTT: {e}"

    def transcribe_with_whisper(audio_path, output_path, structured=True, model_size='base', total_duration=0, check_cancel=None):
        """Transcribe audio file using Whisper AI with optional formatting."""
        if progress_callback:
            progress_callback({'type': 'status', 'msg': f"Initializing Whisper AI ({model_size})..."})
        
        from faster_whisper import WhisperModel
        
        try:
            model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=2)
            
            if progress_callback:
                progress_callback({'type': 'status', 'msg': "Transcribing audio..."})
                
            segments, _ = model.transcribe(audio_path, beam_size=5, vad_filter=False)
        
            with open(output_path, "w", encoding="utf-8") as f:
                first_segment = True
                for segment in segments:
                    text_part = segment.text.strip()
                    if not text_part:
                        continue
                        
                    if structured:
                        timestamp = f"[{int(segment.start // 60):02d}:{int(segment.start % 60):02d}] "
                        f.write(f"{timestamp}{text_part}\n")
                        if text_part.endswith(('.', '!', '?')):
                            f.write("\n")
                    else:
                        if not first_segment:
                            f.write(" ")
                        f.write(text_part)
                        first_segment = False
                    
                    # Send progress update based on audio duration
                    if progress_callback and total_duration > 0:
                        percent = min(99, (segment.end / total_duration) * 100)
                        cur_min, cur_sec = int(segment.end // 60), int(segment.end % 60)
                        tot_min, tot_sec = int(total_duration // 60), int(total_duration % 60)
                        progress_callback({
                            'type': 'progress',
                            'percentage': percent,
                            'status_msg': f"Transcribing: {cur_min:02d}:{cur_sec:02d} / {tot_min:02d}:{tot_sec:02d}"
                        })
                    
                    if check_cancel and check_cancel():
                        raise Exception("Transcription cancelled by user")
            
            if progress_callback:
                progress_callback({'type': 'status', 'msg': "Transcription complete."})
        except Exception as e:
            if progress_callback:
                progress_callback({'type': 'status', 'msg': f"Transcription error: {str(e)}"})
            raise e
        
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
            
            if check_cancel and check_cancel():
                raise Exception("Download cancelled by user")

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

    if media_type == 'subtitles':
        # Prepare for subtitle extraction only
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsubtitles'] = True
        ydl_opts['subtitleslangs'] = ['ru', 'en']
        ydl_opts['skip_download'] = True
        ydl_opts['outtmpl'] = os.path.join(output_path, '%(title)s.%(ext)s')
    
    if media_type == 'transcript':
        # Prepare for whisper transcription - we need the audio
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }]
        ydl_opts['outtmpl'] = os.path.join(output_path, '%(title)s_audio.%(ext)s')

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if metadata_callback:
                metadata_callback(info)
            filename = ydl.prepare_filename(info)
            
            filename = ydl.prepare_filename(info)
            
            if media_type == 'subtitles':
                # Check for subtitles
                subtitle_files = []
                base_name, _ = os.path.splitext(filename)
                for f in os.listdir(output_path):
                    if f.startswith(os.path.basename(base_name)) and f.endswith('.vtt'):
                        subtitle_files.append(os.path.join(output_path, f))
                
                if subtitle_files:
                    transcript_path = base_name + "_subtitles.txt"
                    clean_text = clean_vtt(subtitle_files[0], structured=structured)
                    with open(transcript_path, 'w', encoding='utf-8') as f:
                        f.write(clean_text)
                    for f in subtitle_files:
                        try: os.remove(f)
                        except: pass
                    return os.path.abspath(transcript_path)
                else:
                    raise Exception("No subtitles found on YouTube for this video.")

            if media_type == 'transcript':
                # Use Whisper on the downloaded audio
                base, _ = os.path.splitext(filename)
                if os.path.exists(base + '.mp3'):
                    filename = base + '.mp3'
                
                transcript_path = base + "_transcript.txt"
                duration = info.get('duration', 0)
                transcribe_with_whisper(filename, transcript_path, structured=structured, model_size=model_size, total_duration=duration, check_cancel=check_cancel)
                
                # Cleanup audio file after transcription
                try: os.remove(filename)
                except: pass
                
                return os.path.abspath(transcript_path)

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
            return os.path.abspath(filename)
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
