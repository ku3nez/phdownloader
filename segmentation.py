"""Speaker turns with pyannote segmentation 3.0 (ONNX).

The segmentation model looks at 10-second windows and marks, every ~17 ms,
which of up to three local voices is speaking, including overlapping speech.
For each local voice of a window a WeSpeaker fingerprint is taken from the
frames where only that voice speaks. The final step clusters these
fingerprints over the whole recording; every (window, local voice) gets a
global speaker, and frame-level votes of all overlapping windows say who
speaks at any moment. Speaker turns therefore follow actual voice changes
instead of the 1.5 s windows used before.
"""

import os

import numpy as np

SEGMENTATION_REPO = os.getenv("DIARIZATION_SEGMENTATION_MODEL", "onnx-community/pyannote-segmentation-3.0")
SEGMENTATION_FILE = os.getenv("DIARIZATION_SEGMENTATION_FILE", "onnx/model.onnx")
SAMPLE_RATE = 16000
WINDOW = 10 * SAMPLE_RATE
STEP = int(float(os.getenv("DIARIZATION_SEGMENTATION_STEP", "2.5")) * SAMPLE_RATE)
FRAME_STRIDE = 270
FRAME_FIELD = 991
FRAMES_PER_WINDOW = 589
FRAME_SECONDS = FRAME_STRIDE / SAMPLE_RATE
# Powerset classes: none, {0}, {1}, {2}, {0,1}, {0,2}, {1,2}.
POWERSET = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [1, 0, 1], [0, 1, 1]], dtype=bool)
MIN_EMBED_SECONDS = 0.6
MAX_EMBED_SECONDS = 6.0
BATCH = 8


def _session(repo, filename, cpu_threads):
    import onnxruntime as rt
    from huggingface_hub import hf_hub_download

    options = rt.SessionOptions()
    options.intra_op_num_threads = cpu_threads
    options.inter_op_num_threads = 1
    return rt.InferenceSession(hf_hub_download(repo, filename), options, providers=["CPUExecutionProvider"])


def _frame_span(frame):
    """Samples a frame stands for (centre of its receptive field)."""
    centre = frame * FRAME_STRIDE + FRAME_FIELD // 2
    return centre - FRAME_STRIDE // 2, centre + FRAME_STRIDE // 2


def _window_starts(length):
    if length <= WINDOW:
        return [0]
    starts = list(range(0, length - WINDOW + 1, STEP))
    if starts[-1] + WINDOW < length:
        starts.append(length - WINDOW)
    return starts


def analyse(audio, cpu_threads=2, check_cancel=None, status=None):
    """Segment a 16 kHz signal and fingerprint every local voice.

    Returns a dict of arrays: window starts (s), activity [W, 589, 3] and, per
    local voice with enough clean speech, its window, local index and embedding.
    """
    from diarization import EMBEDDING_FILE, EMBEDDING_REPO, _load_embedding_model

    segmenter = _session(SEGMENTATION_REPO, SEGMENTATION_FILE, cpu_threads)
    embedder, embed_input, preprocessor = _load_embedding_model(cpu_threads)
    starts = _window_starts(len(audio))
    activity = np.zeros((len(starts), FRAMES_PER_WINDOW, 3), dtype=bool)
    emb_window, emb_local, embeddings = [], [], []

    def embed(signal):
        features, _ = preprocessor(signal[None].astype(np.float32), np.array([len(signal)], dtype=np.int64))
        features = features - features.mean(axis=1, keepdims=True)
        return embedder.run(None, {embed_input: features})[0][0]

    for begin in range(0, len(starts), BATCH):
        batch_starts = starts[begin:begin + BATCH]
        batch = np.zeros((len(batch_starts), 1, WINDOW), dtype=np.float32)
        for i, start in enumerate(batch_starts):
            piece = audio[start:start + WINDOW]
            batch[i, 0, :len(piece)] = piece
        logits = segmenter.run(None, {segmenter.get_inputs()[0].name: batch})[0]
        for i, start in enumerate(batch_starts):
            w = begin + i
            frames = POWERSET[np.argmax(logits[i], axis=-1)]
            activity[w] = frames[:FRAMES_PER_WINDOW]
            alone = frames.sum(axis=1) == 1
            for local in range(3):
                clean = np.where(frames[:, local] & alone)[0]
                if len(clean) * FRAME_SECONDS < MIN_EMBED_SECONDS:
                    continue
                spans = [_frame_span(f) for f in clean]
                signal = np.concatenate([audio[start + a:start + b] for a, b in spans if start + a >= 0])
                signal = signal[:int(MAX_EMBED_SECONDS * SAMPLE_RATE)]
                if len(signal) < MIN_EMBED_SECONDS * SAMPLE_RATE:
                    continue
                emb_window.append(w)
                emb_local.append(local)
                embeddings.append(embed(signal))
        if check_cancel and check_cancel():
            raise Exception("Transcription cancelled during speaker analysis")
        if status and begin % (BATCH * 10) == 0:
            status(f"[Speakers] Speaker turns: {begin}/{len(starts)} windows")
    return {
        "starts": np.array(starts, dtype=np.float64) / SAMPLE_RATE,
        "activity": activity,
        "emb_window": np.array(emb_window, dtype=np.int32),
        "emb_local": np.array(emb_local, dtype=np.int8),
        "embeddings": np.array(embeddings, dtype=np.float32).reshape(len(embeddings), -1),
    }


def save(path, result):
    with open(path, "wb") as handle:
        np.savez_compressed(
            handle,
            seg_starts=result["starts"],
            seg_activity=np.packbits(result["activity"], axis=1),
            seg_frames=np.array([result["activity"].shape[1]]),
            seg_emb_window=result["emb_window"],
            seg_emb_local=result["emb_local"],
            seg_embeddings=result["embeddings"].astype(np.float16),
        )


def load(sources):
    """Combine (offset_seconds, path) results into one recording-wide set."""
    starts, activity, emb_window, emb_local, embeddings = [], [], [], [], []
    windows_before = 0
    for offset, path in sources:
        if not path or not os.path.exists(path):
            continue
        with np.load(path) as data:
            if "seg_starts" not in data:
                # A chunk analysed the old way: mixing both is not possible.
                return None
            frames = int(data["seg_frames"][0])
            act = np.unpackbits(data["seg_activity"], axis=1, count=frames).astype(bool)
            starts.append(data["seg_starts"] + float(offset))
            activity.append(act)
            emb_window.append(data["seg_emb_window"].astype(np.int64) + windows_before)
            emb_local.append(data["seg_emb_local"])
            embeddings.append(data["seg_embeddings"].astype(np.float32))
            windows_before += len(data["seg_starts"])
    if not starts or not any(len(e) for e in embeddings):
        return None
    return {
        "starts": np.concatenate(starts),
        "activity": np.concatenate(activity),
        "emb_window": np.concatenate(emb_window),
        "emb_local": np.concatenate(emb_local),
        "embeddings": np.concatenate([e for e in embeddings if len(e)]),
    }


def speaker_timeline(data, labels):
    """Frame-level votes: (frame_times, scores [frames, K+1])."""
    end = data["starts"].max() + WINDOW / SAMPLE_RATE
    total = int(np.ceil(end / FRAME_SECONDS)) + 1
    k = int(labels.max()) if len(labels) else 0
    scores = np.zeros((total, k + 1), dtype=np.float32)
    mapping = {}
    for w, local, label in zip(data["emb_window"], data["emb_local"], labels):
        mapping[(int(w), int(local))] = int(label)
    for (w, local), label in mapping.items():
        frames = np.where(data["activity"][w, :, local])[0]
        if not len(frames):
            continue
        first = int(round(data["starts"][w] / FRAME_SECONDS))
        index = first + frames
        index = index[index < total]
        scores[index, label] += 1.0
    return scores


def speaker_at(scores, t, search_seconds=1.0):
    frame = int(t / FRAME_SECONDS)
    if 0 <= frame < len(scores) and scores[frame].max() > 0:
        return int(np.argmax(scores[frame]))
    reach = int(search_seconds / FRAME_SECONDS)
    lo, hi = max(0, frame - reach), min(len(scores), frame + reach + 1)
    window = scores[lo:hi]
    if len(window) and window.max() > 0:
        row = np.argmax(window.max(axis=1))
        return int(np.argmax(window[row]))
    return None
