"""Stage directions for transcripts: how a paragraph was said.

Just for fun ("Саня (раздражённо, повысив голос): …"). Per recognised segment
the worker measures loudness and, with GigaAM-Emo, the emotion of the voice
(angry / sad / neutral / positive). For every paragraph these are compared
with the speaker's own usual manner, together with the speech rate and text
cues (swearing, imperatives, questions), and at most two remarks are chosen.
"""

import os
import re

import numpy as np

EMO_MODEL_PATH = os.getenv(
    "TONE_EMO_MODEL",
    os.path.join(os.path.abspath(os.getenv("SHARED_STORAGE_ROOT", "downloads")), "models", "gigaam-emo.int8.onnx"),
)
EMO_CLASSES = ("angry", "sad", "neutral", "positive")
SAMPLE_RATE = 16000
EMO_MAX_SECONDS = 10.0
EMO_MIN_SECONDS = 1.0

# --- log-mel features exactly as GigaAM's torchaudio preprocessor ----------
_N_FFT, _HOP, _N_MELS = 400, 160, 64


def _mel_filterbank():
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    freqs = np.linspace(0, SAMPLE_RATE // 2, _N_FFT // 2 + 1)
    points = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(SAMPLE_RATE / 2), _N_MELS + 2))
    slopes = points[None, :] - freqs[:, None]
    down = -slopes[:, :-2] / np.diff(points)[:-1]
    up = slopes[:, 2:] / np.diff(points)[1:]
    return np.maximum(0.0, np.minimum(down, up)).astype(np.float32)


_FILTERBANK = _mel_filterbank()
_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_N_FFT) / _N_FFT)).astype(np.float32)


def _logmel(wav):
    padded = np.pad(wav, (_N_FFT // 2, _N_FFT // 2), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, _N_FFT)[::_HOP] * _WINDOW
    power = np.abs(np.fft.rfft(frames, n=_N_FFT, axis=1)) ** 2
    return np.log(np.clip(power @ _FILTERBANK, 1e-9, 1e9)).T.astype(np.float32)


def segment_tones(audio, segments, cpu_threads=2, check_cancel=None, status=None):
    """[{"db": loudness, "emo": [angry, sad, neutral, positive] or None}] per segment."""
    session = None
    if os.path.exists(EMO_MODEL_PATH):
        import onnxruntime as rt

        options = rt.SessionOptions()
        options.intra_op_num_threads = cpu_threads
        options.inter_op_num_threads = 1
        session = rt.InferenceSession(EMO_MODEL_PATH, options, providers=["CPUExecutionProvider"])
    elif status:
        status(f"WARNING: emotion model not found at {EMO_MODEL_PATH}; remarks use loudness, pace and text only")
    tones = []
    for number, segment in enumerate(segments):
        start, end = int(segment["start"] * SAMPLE_RATE), int(segment["end"] * SAMPLE_RATE)
        piece = audio[start:end]
        if len(piece) == 0:
            tones.append({"db": None, "emo": None})
            continue
        rms = float(np.sqrt(np.mean(piece.astype(np.float64) ** 2)))
        tone = {"db": round(20 * np.log10(max(rms, 1e-6)), 2), "emo": None}
        if session is not None and len(piece) >= EMO_MIN_SECONDS * SAMPLE_RATE:
            limit = int(EMO_MAX_SECONDS * SAMPLE_RATE)
            if len(piece) > limit:  # the middle of a long segment is representative
                offset = (len(piece) - limit) // 2
                piece = piece[offset:offset + limit]
            features = _logmel(piece.astype(np.float32))[None]
            probs = session.run(None, {"features": features, "feature_lengths": np.array([features.shape[2]], dtype=np.int64)})[0][0]
            tone["emo"] = [round(float(p), 3) for p in probs]
        tones.append(tone)
        if check_cancel and number % 20 == 0 and check_cancel():
            raise Exception("Transcription cancelled during tone analysis")
        if status and number % 50 == 0:
            status(f"[Speakers] Tone of voice: {number}/{len(segments)}")
    return tones


# --- choosing remarks --------------------------------------------------------
PROFANITY_RE = re.compile(
    # Explicit prefixes only: "\w+еб..." would also catch "хлебал", "колебать".
    r"\b(?:(?:о|на|по|за|при)?ху[йеёияю]\w*|\w*пизд\w*|[её]б[аеёиоуы]\w*|(?:за|у|на|вы|от|отъ|об|объ|до|про|подъ|разъ|съ|въ|пере|при)[её]б\w*|бля\w*|муда[кч]\w*|мудил\w*|"
    r"пидо?р\w*|сук[аиуе]\b|сучк\w*|залуп\w*|fuck\w*|shit\w*|bitch\w*)",
    re.I,
)
REMARKS = {
    "swear_shout": ("орёт матом", "yelling and swearing"),
    "swear": ("с матом", "swearing"),
    "fury": ("в ярости", "furiously"),
    "angry": ("раздражённо", "irritably"),
    "sad": ("грустно", "sadly"),
    "excited": ("воодушевлённо", "enthusiastically"),
    "cheerful": ("весело", "cheerfully"),
    "loud": ("повысив голос", "raising their voice"),
    "quiet": ("вполголоса", "quietly"),
    "fast": ("тараторя", "rattling off"),
    "slow": ("с расстановкой", "deliberately"),
    "demanding": ("требовательно", "demandingly"),
    "pressing": ("допытываясь", "pressingly"),
    "didactic": ("назидательно", "didactically"),
    "heated": ("с жаром", "heatedly"),
}

_morph = None


def _imperatives(text):
    global _morph
    try:
        if _morph is None:
            import pymorphy3
            _morph = pymorphy3.MorphAnalyzer()
    except Exception:
        return 0
    count = 0
    for word in re.findall(r"[А-Яа-яЁё]+", text):
        parse = _morph.parse(word)[0]
        if "impr" in parse.tag and parse.score >= 0.4:
            count += 1
    return count


def choose_remarks(stats, baseline, text, russian=True, limit=2):
    """stats/baseline: {"db", "rate", "emo"}; returns remark words for a paragraph."""
    picked = []
    emo = stats.get("emo")
    angry, sad, positive = (emo[0], emo[1], emo[3]) if emo else (0.0, 0.0, 0.0)
    louder = (stats["db"] - baseline["db"]) if stats.get("db") is not None and baseline.get("db") is not None else 0.0
    pace = (stats["rate"] / baseline["rate"]) if stats.get("rate") and baseline.get("rate") else 1.0
    swears = len(PROFANITY_RE.findall(text))

    if swears and (louder >= 4 or angry >= 0.5):
        picked.append("swear_shout")
    elif swears:
        picked.append("swear")
    if angry >= 0.7 and louder >= 4:
        picked.append("fury")
    elif angry >= 0.45:
        picked.append("angry")
    elif sad >= 0.45:
        picked.append("sad")
    elif positive >= 0.5:
        picked.append("excited" if pace >= 1.2 else "cheerful")
    if louder >= 5 and "swear_shout" not in picked and "fury" not in picked:
        picked.append("loud")
    elif louder <= -6:
        picked.append("quiet")
    if pace >= 1.35:
        picked.append("fast")
    elif pace <= 0.7:
        picked.append("didactic" if len(text) > 500 and not emo_is_emotional(emo) else "slow")
    if _imperatives(text) >= 2:
        picked.append("demanding")
    if text.count("?") >= 3:
        picked.append("pressing")
    if text.count("!") >= 2:
        picked.append("heated")
    seen, result = set(), []
    for key in picked:
        if key not in seen:
            seen.add(key)
            result.append(REMARKS[key][0 if russian else 1])
    return result[:limit]


def emo_is_emotional(emo):
    return bool(emo) and max(emo[0], emo[1], emo[3]) >= 0.45


def paragraph_stats(paragraph_words, segments):
    """Loudness, pace and emotion of a paragraph from its words' segments."""
    by_segment = {}
    for word in paragraph_words:
        by_segment[word["seg"]] = by_segment.get(word["seg"], 0) + 1
    db_sum = weight = speech = 0.0
    emo_sum = np.zeros(4)
    emo_weight = 0.0
    for index, count in by_segment.items():
        segment = segments[index]
        total_words = max(1, len(segment.get("words") or []) or count)
        share = min(1.0, count / total_words)
        speech += share * max(0.0, float(segment.get("end", 0)) - float(segment.get("start", 0)))
        tone = segment.get("tone") or {}
        if tone.get("db") is not None:
            db_sum += tone["db"] * count
            weight += count
        if tone.get("emo"):
            emo_sum += np.array(tone["emo"]) * count
            emo_weight += count
    words = sum(by_segment.values())
    return {
        "db": db_sum / weight if weight else None,
        "rate": words / speech if speech > 0.5 else None,
        "emo": list(emo_sum / emo_weight) if emo_weight else None,
    }
