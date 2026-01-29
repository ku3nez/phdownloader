import yt_dlp
import sys
import os

def download_video(url, output_path='downloads', quality='720', progress_callback=None):
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    import re
    def strip_ansi(text):
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    class YdlLogger:
        def debug(self, msg):
            if progress_callback:
                progress_callback({'type': 'status', 'msg': strip_ansi(msg)})
        def warning(self, msg):
            if progress_callback:
                progress_callback({'type': 'status', 'msg': f"WARNING: {strip_ansi(msg)}"})
        def error(self, msg):
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

    # Format mapping for better control
    if quality == 'best':
        format_str = 'bestvideo+bestaudio/best'
    else:
        # Try to get the specific resolution or the next best thing below it
        format_str = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]'

    quality_suffix = f"_{quality}p" if quality != 'best' else "_best"
    
    ydl_opts = {
        'format': format_str,
        'outtmpl': os.path.join(output_path, f'%(title)s{quality_suffix}.%(ext)s'),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': False,
        'logger': YdlLogger(),
        'concurrent_fragment_downloads': 32,
        'retries': 15,
        'fragment_retries': 15,
        'socket_timeout': 60,
        'nocontinue': False, # Allow resume if possible, though 'nocontinue': True was there before
        'hls_prefer_native': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'skip': ['hls', 'dash']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
            'Sec-Fetch-Mode': 'navigate',
        },
        'progress_hooks': [hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # If it was merged, the extension might have changed to mp4
            if not os.path.exists(filename):
                base, _ = os.path.splitext(filename)
                if os.path.exists(base + '.mp4'):
                    filename = base + '.mp4'
            return filename
    except Exception as e:
        if progress_callback:
            progress_callback({'type': 'status', 'msg': f"ERROR: {str(e)}"})
        return None

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python downloader.py <URL>")
        # Default for testing if no URL provided
        video_url = "https://www.pornhub.com/view_video.php?viewkey=66a8e6f727b86"
        print(f"Using default test URL: {video_url}")
    else:
        video_url = sys.argv[1]
    
    download_video(video_url)
