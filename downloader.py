import yt_dlp
import sys
import os
import traceback
import time
from dotenv import load_dotenv
import builtins

def safe_print(*args, **kwargs):
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = [
            str(arg).encode('ascii', errors='replace').decode('ascii')
            for arg in args
        ]
        builtins.print(*safe_args, **kwargs)

print = safe_print

# Load environment variables
load_dotenv()
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    import curl_cffi
except ImportError:
    ImpersonateTarget = None

def get_media_duration(file_path):
    """Get media duration in seconds using ffprobe."""
    import subprocess
    try:
        cmd = [
            'ffprobe', 
            '-v', 'error', 
            '-show_entries', 'format=duration', 
            '-of', 'default=noprint_wrappers=1:nokey=1', 
            file_path
        ]
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, startupinfo=startupinfo)
        duration_str = result.stdout.strip()
        if duration_str:
            return float(duration_str)
    except Exception as e:
        print(f"Error getting duration with ffprobe: {e}")
    return 0.0

def transcribe_with_whisper(audio_path, output_path, structured=True, model_size='base', total_duration=0, progress_callback=None, check_cancel=None, return_segments=False):
    """Transcribe audio file using Whisper AI with optional formatting."""
    if progress_callback:
        progress_callback({'type': 'status', 'msg': "Preparing audio..."})
        
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    import numpy as np
    import logging
    import threading
    import queue
    import time
    
    # Configure faster_whisper logger capture to send logs to the client
    fw_logger = logging.getLogger("faster_whisper")
    original_level = fw_logger.level
    fw_logger.setLevel(logging.INFO)
    
    class TaskLogHandler(logging.Handler):
        def __init__(self, callback):
            super().__init__()
            self.callback = callback
        def emit(self, record):
            try:
                msg = self.format(record)
                if self.callback:
                    self.callback({'type': 'status', 'msg': f"[Whisper] {msg}"})
            except:
                pass
                
    handler = TaskLogHandler(progress_callback)
    handler.setFormatter(logging.Formatter('%(message)s'))
    fw_logger.addHandler(handler)
    
    try:
        # Load and decode audio to 16000Hz mono float32
        try:
            audio_samples = decode_audio(audio_path, sampling_rate=16000)
            
            # Check peak amplitude and auto-amplify if too quiet
            peak = np.max(np.abs(audio_samples)) if len(audio_samples) > 0 else 0.0
            if 0.0 < peak < 0.15:
                scale = 0.8 / peak
                audio_samples = audio_samples * scale
                msg = f"Auto-amplified quiet audio in-memory by factor of {scale:.2f} (+{20 * np.log10(scale):.1f} dB)"
                print(msg)
                if progress_callback:
                    progress_callback({'type': 'status', 'msg': msg})
        except Exception as e:
            msg = f"Error preparing audio, falling back to original file path: {e}"
            print(msg)
            if progress_callback:
                progress_callback({'type': 'status', 'msg': msg})
            audio_samples = audio_path

        if check_cancel and check_cancel():
            raise Exception("Transcription cancelled before Whisper init")
            
        # Get thread count from environment
        threads_env = os.getenv('WHISPER_CPU_THREADS')
        cpu_threads = int(threads_env) if threads_env and threads_env.isdigit() else 2
        
        msg = f"Initializing Whisper Model: size={model_size}, threads={cpu_threads}, compute=int8"
        print(msg)
        if progress_callback:
            progress_callback({'type': 'status', 'msg': msg})
            
        model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=cpu_threads)
        
        if check_cancel and check_cancel():
            raise Exception("Transcription cancelled after Whisper init")
        
        msg = "Transcribing audio (Voice Activity Detection enabled)..."
        print(msg)
        if progress_callback:
            progress_callback({'type': 'status', 'msg': msg})
            
        segments, _ = model.transcribe(audio_samples, beam_size=5, vad_filter=True)
        if progress_callback:
            progress_callback({'type': 'status', 'msg': "[Whisper] Transcription iterator created, waiting for first segment..."})

        segment_queue = queue.Queue()
        worker_error = []
        worker_done = threading.Event()

        def collect_segments():
            try:
                for segment in segments:
                    segment_queue.put(segment)
            except Exception as exc:
                worker_error.append(exc)
            finally:
                worker_done.set()

        worker = threading.Thread(target=collect_segments, daemon=True)
        worker.start()
    
        segments_data = []
        with open(output_path, "w", encoding="utf-8") as f:
            first_segment = True
            last_logged_percent = -1.0
            first_segment_seen = False
            wait_started_at = time.monotonic()
            last_heartbeat_at = wait_started_at
            while True:
                try:
                    segment = segment_queue.get(timeout=5)
                except queue.Empty:
                    now = time.monotonic()
                    if worker_done.is_set():
                        break
                    if progress_callback and now - last_heartbeat_at >= 15:
                        elapsed = int(now - wait_started_at)
                        progress_callback({
                            'type': 'status',
                            'msg': f"[Whisper] Still processing, no segments emitted yet ({elapsed}s elapsed)"
                                if not first_segment_seen else
                                f"[Whisper] Still processing remaining audio ({elapsed}s since transcription started)"
                        })
                        last_heartbeat_at = now
                    if check_cancel and check_cancel():
                        raise Exception("Transcription cancelled while waiting for Whisper output")
                    continue

                if not first_segment_seen and progress_callback:
                    elapsed = int(time.monotonic() - wait_started_at)
                    progress_callback({'type': 'status', 'msg': f"[Whisper] First segment received after {elapsed}s"})
                first_segment_seen = True
                text_part = segment.text.strip()
                if not text_part:
                    continue
                segments_data.append({
                    'start': float(segment.start),
                    'end': float(segment.end),
                    'text': text_part,
                })
                    
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
                    percent = min(99.0, (segment.end / total_duration) * 100.0)
                    cur_min, cur_sec = int(segment.end // 60), int(segment.end % 60)
                    tot_min, tot_sec = int(total_duration // 60), int(total_duration % 60)
                    progress_callback({
                        'type': 'progress',
                        'percentage': percent,
                        'status_msg': f"Transcribing: {cur_min:02d}:{cur_sec:02d} / {tot_min:02d}:{tot_sec:02d}"
                    })
                    # Log progress to the text console if it changes by >= 1% or it's the first segment
                    if percent - last_logged_percent >= 1.0 or last_logged_percent < 0:
                        progress_callback({
                            'type': 'status',
                            'msg': f"Progress: {cur_min:02d}:{cur_sec:02d} / {tot_min:02d}:{tot_sec:02d} ({percent:.1f}%)"
                        })
                        last_logged_percent = percent
                
                if check_cancel and check_cancel():
                    print(f"Stopping transcription loop for user request")
                    raise Exception("Transcription cancelled by user")

            if worker_error:
                raise worker_error[0]
        
        if progress_callback:
            progress_callback({'type': 'status', 'msg': "Transcription complete."})
    except Exception as e:
        if progress_callback:
            progress_callback({'type': 'status', 'msg': f"Transcription error: {str(e)}"})
        raise e
    finally:
        try:
            fw_logger.removeHandler(handler)
            fw_logger.setLevel(original_level)
        except:
            pass
            
    if return_segments:
        return segments_data
    return output_path

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

    cookie_file = os.getenv('YT_DLP_COOKIE_FILE', 'cookies.txt')
    cookies_browser = os.getenv('YT_DLP_COOKIES_BROWSER')
    js_runtime = os.getenv('YT_DLP_JS_RUNTIME', 'node')
    proxy = os.getenv('YT_DLP_PROXY')  # e.g. socks5://127.0.0.1:1080 or http://host:port
    fragment_concurrency_raw = os.getenv('YT_DLP_CONCURRENT_FRAGMENT_DOWNLOADS', '4')
    try:
        fragment_concurrency = int(fragment_concurrency_raw)
    except ValueError:
        fragment_concurrency = 4
    fragment_concurrency = max(1, min(fragment_concurrency, 32))

    active_cookie_file = cookie_file if cookie_file and os.path.exists(cookie_file) else None

    # Validate that the browser cookie DB actually exists before trying to use it
    # (fails on VPS/servers where the browser is not installed)
    active_cookies_browser = None
    if cookies_browser:
        import glob
        browser_paths = {
            'chrome': [
                os.path.expanduser('~/.config/google-chrome'),
                os.path.expanduser('~/.config/chromium'),
                os.path.expandvars('%LOCALAPPDATA%/Google/Chrome/User Data'),
            ],
            'firefox': [
                os.path.expanduser('~/.mozilla/firefox'),
                os.path.expandvars('%APPDATA%/Mozilla/Firefox/Profiles'),
            ],
            'chromium': [
                os.path.expanduser('~/.config/chromium'),
            ],
        }
        browser_key = cookies_browser.lower()
        paths_to_check = browser_paths.get(browser_key, [])
        browser_available = any(os.path.exists(p) for p in paths_to_check)
        if browser_available:
            active_cookies_browser = cookies_browser
            print(f"Using browser cookies from: {cookies_browser}")
        else:
            print(f"WARNING: Browser '{cookies_browser}' not found on this system, skipping browser cookies.")
            if progress_callback:
                progress_callback({'type': 'status', 'msg': f"WARNING: Browser '{cookies_browser}' not found, trying without browser cookies..."})

    import shutil
    has_aria2 = shutil.which('aria2c') is not None
    if has_aria2:
        print("Using external downloader: aria2c (multithreaded)")

    is_youtube = 'youtube.com' in url.lower() or 'youtu.be' in url.lower()
    is_ph = 'pornhub.com' in url.lower()

    ydl_opts = {
        'external_downloader': 'aria2c' if has_aria2 else None,
        'external_downloader_args': {
            'aria2c': ['-x', '16', '-s', '16', '-k', '1M', '--min-split-size=1M']
        } if has_aria2 else None,
        'noplaylist': True,
        'quiet': False,
        'logger': YdlLogger(),
        # HLS videos such as PornHub streams use the native fragment downloader;
        # aria2 cannot increase their transfer concurrency.
        'concurrent_fragment_downloads': fragment_concurrency,
        'retries': 30,
        'fragment_retries': 30,
        'retry_sleep_functions': {'http': lambda n: 5 * 2 ** n},
        'socket_timeout': 60,
        'nocontinue': False,
        'hls_prefer_native': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'log_t_steps': True,
        'extractor_args': {
            'youtube': {
                'skip': ['po_token'] if active_cookie_file or active_cookies_browser else []
            }
        } if is_youtube else (
            {
                'pornhub': {
                    'age_verified': ['1'],
                }
            } if is_ph else {}
        ),
        'impersonate': ImpersonateTarget.from_str('chrome') if ImpersonateTarget else None,
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Referer': 'https://www.youtube.com/' if is_youtube else ('https://www.pornhub.com/' if is_ph else url),
        },
        'progress_hooks': [hook],
        'cookiesfrombrowser': (active_cookies_browser,) if active_cookies_browser and not active_cookie_file else None,
        'cookiefile': active_cookie_file,
        'js_runtimes': {js_runtime: {}} if js_runtime else None,
        'remote_components': ['ejs:github'],
        'proxy': proxy if proxy else None,
    }

    if media_type == 'audio':
        # Handle audio quality selection (bitrate)
        if quality and quality.isdigit():
            audio_bitrate = quality
        else:
            audio_bitrate = '192' # Default
            
        # Prefer quality up to the selected bitrate to save bandwidth
        ydl_opts['format'] = f'bestaudio[abr<={audio_bitrate}]/bestaudio[ext=m4a]/bestaudio/best[height<=360]/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': audio_bitrate,
        }]
        ydl_opts['outtmpl'] = os.path.join(output_path, '%(title)s_audio.%(ext)s')
    else:
        # Format mapping for better control
        if quality == 'best':
            format_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        else:
            # Prefer MP4 video + M4A audio so yt-dlp does not fall back to a low progressive format.
            format_str = (
                f'bestvideo[ext=mp4][height<={quality}]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={quality}]+bestaudio/'
                f'best[ext=mp4][height<={quality}]/best[height<={quality}]'
            )

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
        # Prepare for whisper transcription - we need the audio; use low quality to save weight
        ydl_opts['format'] = 'bestaudio[abr<=64]/bestaudio[ext=m4a]/bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }]
        ydl_opts['outtmpl'] = os.path.join(output_path, '%(title)s_audio.%(ext)s')

    info = None
    ydl_instance = None
    retry_without_cookies = False
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                ydl_instance = ydl
                requested_formats = info.get('requested_formats') or []
                if requested_formats:
                    selected = "+".join(str(item.get("format_id")) for item in requested_formats if item.get("format_id"))
                else:
                    selected = str(info.get("format_id") or "unknown")
                msg = f"Selected format: {selected}, ext={info.get('ext')}"
                print(msg)
                if progress_callback:
                    progress_callback({'type': 'status', 'msg': msg})
                break
        except Exception as e:
            error_str = str(e)
            lower_error = error_str.lower()
            if attempt == 0 and ydl_opts.get('impersonate') and ('Impersonate target' in error_str or 'curl_cffi' in error_str):
                print("WARNING: Impersonation is not available on this system. Retrying without impersonation...")
                if progress_callback:
                    progress_callback({'type': 'status', 'msg': "WARNING: Impersonation not supported. Retrying..."})
                ydl_opts['impersonate'] = None
                continue
            if (
                is_youtube
                and not retry_without_cookies
                and ('cookies are no longer valid' in lower_error or 'failed to extract any player response' in lower_error)
                and (ydl_opts.get('cookiefile') or ydl_opts.get('cookiesfrombrowser'))
            ):
                print("WARNING: YouTube cookies appear invalid. Retrying without cookies...")
                if progress_callback:
                    progress_callback({'type': 'status', 'msg': "WARNING: YouTube cookies appear invalid. Retrying without cookies..."})
                ydl_opts['cookiefile'] = None
                ydl_opts['cookiesfrombrowser'] = None
                ydl_opts['extractor_args'] = {'youtube': {}}
                retry_without_cookies = True
                continue
            if (
                is_youtube
                and attempt < 2
                and ('failed to extract any player response' in lower_error or 'failed to parse json' in lower_error)
            ):
                delay = 2 * (attempt + 1)
                print(f"WARNING: Transient YouTube extraction failure. Retrying in {delay}s...")
                if progress_callback:
                    progress_callback({'type': 'status', 'msg': f"WARNING: Transient YouTube extraction failure. Retrying in {delay}s..."})
                time.sleep(delay)
                continue
            
            # General cleanup try for any partial files
            pass
            
            error_msg = f"{error_str}"
            if not error_msg.strip():
                error_msg = f"Unknown Error: {type(e).__name__}"
            
            # Provide helpful hints for common errors
            if 'HTTP Error 403' in error_msg or 'Forbidden' in error_msg:
                if is_ph:
                    error_msg = (
                        "HTTP Error 403: PornHub requires browser cookies for access.\n"
                        "Set YT_DLP_COOKIES_BROWSER=chrome (or firefox) in your .env file, "
                        "or export cookies to cookies.txt and set YT_DLP_COOKIE_FILE=cookies.txt"
                    )
                elif is_youtube:
                    error_msg = (
                        "HTTP Error 403: YouTube is blocking the request.\n"
                        "Try setting YT_DLP_COOKIES_BROWSER=chrome in your .env file "
                        "to use your browser session cookies."
                    )
            
            full_tb = traceback.format_exc()
            print(f"DOWNLOAD EXCEPTION CAUGHT:\n{full_tb}")
            
            if progress_callback:
                progress_callback({'type': 'status', 'msg': f"ERROR: {error_msg}"})
                progress_callback({'type': 'status', 'msg': "Check server console for full traceback."})
            # Propagate the extractor error to the RQ job. Returning None masks
            # the actual cause with the misleading "without output file" error.
            raise RuntimeError(error_msg) from e

    if info and ydl_instance:
        if metadata_callback:
            metadata_callback(info)
        filename = ydl_instance.prepare_filename(info)
        
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
            if progress_callback:
                progress_callback({'type': 'status', 'msg': 'Preparing audio file...'})
            
            duration = info.get('duration', 0)
            transcribe_with_whisper(filename, transcript_path, structured=structured, model_size=model_size, total_duration=duration, progress_callback=progress_callback, check_cancel=check_cancel)
            
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
