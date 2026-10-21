"""Speaker diarization: who spoke when.

Each recognition job extracts speaker embeddings ("voice fingerprints") on
short sliding windows of its speech and saves them next to its segments. The
final step clusters the windows of the whole recording at once, so speaker
numbers stay consistent across chunks processed on different nodes, and then
gives every recognised word the speaker of the nearest window.
"""

import os
import re

import numpy as np

EMBEDDING_REPO = os.getenv("DIARIZATION_EMBEDDING_MODEL", "onnx-community/wespeaker-voxceleb-resnet34-LM")
EMBEDDING_FILE = os.getenv("DIARIZATION_EMBEDDING_FILE", "onnx/model.onnx")
# Cosine distance at which two groups of windows count as different voices.
CLUSTER_THRESHOLD = float(os.getenv("DIARIZATION_THRESHOLD", "0.7"))
WINDOW_SECONDS = 1.5
HOP_SECONDS = 0.75
MIN_WINDOW_SECONDS = 0.5
BATCH_SIZE = 16
# Hierarchical clustering is quadratic in memory; longer recordings are
# clustered on an even sample and the remaining windows join the nearest voice.
MAX_CLUSTER_WINDOWS = 3000
# Spectral clustering: each window keeps edges to this share of its most
# similar windows; at most this many speakers are detected automatically.
PRUNE_FRACTION = 0.1
MAX_SPEAKERS = 10
# Without a given count the windows are first split into OVERCLUSTER_K groups,
# then groups whose average voices are closer than MERGE_SIMILARITY (cosine)
# are joined. On a 1.5-hour meeting with about ten people the eigengap found
# only 5 voices (all women in one), while this gave 10 at thresholds 0.6-0.8.
OVERCLUSTER_K = int(os.getenv("DIARIZATION_OVERCLUSTER_K", "12"))
MERGE_SIMILARITY = float(os.getenv("DIARIZATION_MERGE_SIMILARITY", "0.7"))
# With a given count, groups this similar are the same person split in two
# (0.94 on that meeting, while different people stayed below 0.55).
DUPLICATE_SIMILARITY = float(os.getenv("DIARIZATION_DUPLICATE_SIMILARITY", "0.8"))
MIN_SPEAKER_SHARE = 0.01
# Words of one phrase (no pause longer than this and no sentence end) share a
# speaker, so the label cannot change mid-sentence.
PHRASE_GAP_SECONDS = 1.0
SAMPLE_RATE = 16000


SENTENCE_END = re.compile(r"[.!?…]$")
# Hesitation sounds: "э", "э-э", "а-а", "м-м", "эм", "хм" (a plain "а" is a word).
FILLER_RE = re.compile(r"^(?:э+|э-э+|а-а+|аа+|м+|м-м+|э?мм+|эм+|хм+|uh+|um+|erm+)[,.!?…]*$", re.I)


def speaker_label(number, russian=True):
    return f"{'Спикер' if russian else 'Speaker'} {number}"


def format_timestamp(seconds):
    total = max(0, int(seconds))
    hours, minutes, secs = total // 3600, (total % 3600) // 60, total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def speech_windows(spans):
    """Sliding windows over speech spans (seconds)."""
    windows = []
    for start, end in spans:
        length = end - start
        if length < MIN_WINDOW_SECONDS:
            continue
        if length <= WINDOW_SECONDS:
            windows.append((start, end))
            continue
        position = start
        while position + WINDOW_SECONDS < end:
            windows.append((position, position + WINDOW_SECONDS))
            position += HOP_SECONDS
        windows.append((end - WINDOW_SECONDS, end))
    return windows


def _load_embedding_model(cpu_threads):
    import onnxruntime as rt
    from huggingface_hub import hf_hub_download
    from onnx_asr.preprocessors.numpy_preprocessor import KaldiPreprocessorNumpy

    options = rt.SessionOptions()
    options.intra_op_num_threads = cpu_threads
    options.inter_op_num_threads = 1
    session = rt.InferenceSession(hf_hub_download(EMBEDDING_REPO, EMBEDDING_FILE), options, providers=["CPUExecutionProvider"])
    return session, session.get_inputs()[0].name, KaldiPreprocessorNumpy("wespeaker")


def compute_embeddings(audio, spans, cpu_threads=2, check_cancel=None, status=None):
    """Return (windows [N,2], embeddings [N,D]) for speech spans of a 16 kHz signal."""
    windows = speech_windows(spans)
    if not windows:
        return np.zeros((0, 2), dtype=np.float32), np.zeros((0, 0), dtype=np.float32)
    session, input_name, preprocessor = _load_embedding_model(cpu_threads)

    def embed(batch):
        lengths = np.array([w.shape[0] for w in batch], dtype=np.int64)
        features, _ = preprocessor(np.stack(batch).astype(np.float32), lengths)
        features = features - features.mean(axis=1, keepdims=True)
        return session.run(None, {input_name: features})[0]

    embeddings = [None] * len(windows)
    full = [i for i, (s, e) in enumerate(windows) if abs((e - s) - WINDOW_SECONDS) < 1e-6]
    full_set = set(full)
    short = [i for i in range(len(windows)) if i not in full_set]
    done = 0
    for begin in range(0, len(full), BATCH_SIZE):
        ids = full[begin:begin + BATCH_SIZE]
        batch = [audio[int(windows[i][0] * SAMPLE_RATE):int(windows[i][0] * SAMPLE_RATE) + int(WINDOW_SECONDS * SAMPLE_RATE)] for i in ids]
        if any(len(w) != int(WINDOW_SECONDS * SAMPLE_RATE) for w in batch):
            short.extend(ids)
            continue
        for i, vector in zip(ids, embed(batch)):
            embeddings[i] = vector
        done += len(ids)
        if check_cancel and check_cancel():
            raise Exception("Transcription cancelled during speaker analysis")
        if status and begin % (BATCH_SIZE * 20) == 0:
            status(f"[Speakers] Voice fingerprints: {done}/{len(windows)}")
    for i in short:
        start, end = windows[i]
        segment = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
        if len(segment) < int(MIN_WINDOW_SECONDS * SAMPLE_RATE):
            continue
        embeddings[i] = embed([segment])[0]
    keep = [i for i, vector in enumerate(embeddings) if vector is not None]
    return (np.array([windows[i] for i in keep], dtype=np.float32),
            np.array([embeddings[i] for i in keep], dtype=np.float32))


def save_embeddings(path, windows, embeddings):
    with open(path, "wb") as handle:
        np.savez_compressed(handle, windows=windows.astype(np.float32), embeddings=embeddings.astype(np.float16))


def load_embeddings(sources):
    """Load (offset_seconds, path) pairs into one time-ordered set."""
    all_windows, all_embeddings = [], []
    for offset, path in sources:
        if not path or not os.path.exists(path):
            continue
        with np.load(path) as data:
            windows, embeddings = data["windows"], data["embeddings"]
        if len(windows):
            all_windows.append(windows.astype(np.float64) + float(offset))
            all_embeddings.append(embeddings.astype(np.float32))
    if not all_windows:
        return np.zeros((0, 2)), np.zeros((0, 0), dtype=np.float32)
    windows = np.concatenate(all_windows)
    embeddings = np.concatenate(all_embeddings)
    order = np.argsort(windows[:, 0], kind="stable")
    return windows[order], embeddings[order]


def _spectral_labels(vectors, num_speakers=None, max_speakers=MAX_SPEAKERS):
    """Spectral clustering on a pruned cosine-affinity graph.

    Without a given count, the number of speakers is the position of the
    largest gap between the smallest eigenvalues of the graph Laplacian.
    Average-linkage clustering with a fixed threshold merged two clear voices
    in an hour-long interview through chains of noisy windows (laughter,
    cross-talk); the eigengap found 1, 2 and 6 speakers correctly on a
    lecture, that interview and a round table.
    """
    from scipy.cluster.vq import kmeans2
    from scipy.sparse.csgraph import laplacian

    count = len(vectors)
    similarity = np.clip(vectors @ vectors.T, 0.0, None)
    keep = max(5, int(PRUNE_FRACTION * count))
    pruned = np.zeros_like(similarity)
    nearest = np.argpartition(-similarity, keep - 1, axis=1)[:, :keep]
    rows = np.arange(count)[:, None]
    pruned[rows, nearest] = similarity[rows, nearest]
    pruned = (pruned + pruned.T) / 2
    values, vectors_lap = np.linalg.eigh(laplacian(pruned, normed=True))
    if num_speakers:
        k = min(int(num_speakers), count)
    else:
        gaps = np.diff(values[:max_speakers + 1])
        k = int(np.argmax(gaps)) + 1
    if k <= 1:
        return np.zeros(count, dtype=int)
    spectral = vectors_lap[:, :k]
    spectral = spectral / np.maximum(np.linalg.norm(spectral, axis=1, keepdims=True), 1e-9)
    _, labels = kmeans2(spectral, k, minit="++", seed=0, iter=50)
    return labels


def _merge_similar(vectors, labels, min_similarity):
    """Join groups whose mean voices are more similar than min_similarity."""
    from scipy.cluster.hierarchy import fcluster, linkage

    ids = list(np.unique(labels))
    if len(ids) < 2:
        return labels
    centroids = np.array([vectors[labels == i].mean(axis=0) for i in ids])
    centroids /= np.maximum(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-9)
    groups = fcluster(linkage(centroids, method="average", metric="cosine"), t=1 - min_similarity, criterion="distance")
    mapping = {ids[j]: int(groups[j]) for j in range(len(ids))}
    return np.array([mapping[label] for label in labels])


def cluster_speakers(embeddings, num_speakers=None, threshold=CLUSTER_THRESHOLD):
    """Speaker number (1..K, by first appearance) for every window."""
    from scipy.cluster.hierarchy import fcluster, linkage

    count = len(embeddings)
    if count == 0:
        return np.zeros(0, dtype=int)
    if count == 1:
        return np.ones(1, dtype=int)
    vectors = embeddings / np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9)
    sample = np.linspace(0, count - 1, min(count, MAX_CLUSTER_WINDOWS)).astype(int)
    if len(sample) >= 40:
        if num_speakers:
            labels = _spectral_labels(vectors[sample], num_speakers)
            labels = _merge_similar(vectors[sample], labels, DUPLICATE_SIMILARITY)
        else:
            k_over = max(2, min(OVERCLUSTER_K, len(sample) // 30))
            labels = _spectral_labels(vectors[sample], k_over)
            labels = _merge_similar(vectors[sample], labels, MERGE_SIMILARITY)
    else:
        # Too few windows for a meaningful graph: plain hierarchical clustering.
        tree = linkage(vectors[sample], method="average", metric="cosine")
        if num_speakers:
            labels = fcluster(tree, t=min(int(num_speakers), len(sample)), criterion="maxclust")
        else:
            labels = fcluster(tree, t=threshold, criterion="distance")
    groups = [k for k in np.unique(labels)]
    sizes = np.array([(labels == k).sum() for k in groups])
    if not num_speakers:
        # A handful of windows (coughs, laughter, music) is not a speaker.
        keep = sizes >= max(2, int(MIN_SPEAKER_SHARE * len(sample)))
        if not keep.any():
            keep = sizes == sizes.max()
        groups = [k for k, kept in zip(groups, keep) if kept]
    centroids = np.array([vectors[sample][labels == k].mean(axis=0) for k in groups])
    centroids /= np.maximum(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-9)
    assigned = np.argmax(vectors @ centroids.T, axis=1)
    numbering = {}
    for group in assigned:
        numbering.setdefault(int(group), len(numbering) + 1)
    return np.array([numbering[int(group)] for group in assigned])


def segment_words(segment):
    """Words with absolute start times from a stored segment."""
    words = segment.get("words")
    if words:
        return [w for w in words if str(w.get("w", "")).strip()]
    text = str(segment.get("text", "")).strip()
    return [{"t": float(segment.get("start", 0) or 0), "w": text}] if text else []


def _majority(result, indexes):
    counts = {}
    for i in indexes:
        counts[result[i]] = counts.get(result[i], 0) + 1
    winner = max(counts, key=counts.get)
    for i in indexes:
        result[i] = winner


def assign_speakers(words, windows, labels):
    if not len(windows):
        return [1] * len(words)
    centers = windows.mean(axis=1)
    order = np.argsort(centers)
    centers, labels = centers[order], labels[order]
    result = []
    for word in words:
        position = np.searchsorted(centers, word["t"])
        candidates = [i for i in (position - 1, position) if 0 <= i < len(centers)]
        nearest = min(candidates, key=lambda i: abs(centers[i] - word["t"]))
        result.append(int(labels[nearest]))
    # Speaker turns change at pauses or sentence ends: give every phrase the
    # speaker of most of its words ("Спикер 4: потому что у Билайна /
    # Спикер 6: проблемы с отчётами" was one sentence).
    phrase = []
    for i, word in enumerate(words):
        if phrase:
            previous = words[i - 1]
            if (word.get("seg") != previous.get("seg") or word["t"] - previous["t"] > PHRASE_GAP_SECONDS
                    or SENTENCE_END.search(str(previous["w"]))):
                _majority(result, phrase)
                phrase = []
        phrase.append(i)
    if phrase:
        _majority(result, phrase)
    # A single word between two words of the same speaker is almost always a
    # boundary artefact of the 1.5 s windows.
    for i in range(1, len(result) - 1):
        if result[i - 1] == result[i + 1] != result[i]:
            result[i] = result[i - 1]
    return result


# Paragraphs: a new one starts on a speaker change, after a pause longer than
# PARAGRAPH_PAUSE seconds, or at the first sentence end past PARAGRAPH_CHARS.
PARAGRAPH_PAUSE = 2.0
PARAGRAPH_CHARS = 600
PARAGRAPH_HARD_CHARS = 1200
BACKCHANNEL_WORDS = {
    "угу", "ага", "да", "ну", "ясно", "понятно", "хорошо", "окей", "ок", "конечно", "так", "точно", "верно", "правильно",
    "ммм", "мм", "yes", "yeah", "ok", "okay", "right", "sure", "mhm",
}


def _is_backchannel(text):
    words = re.findall(r"\w+", text.lower())
    return 0 < len(words) <= 3 and all(w in BACKCHANNEL_WORDS for w in words)


def _paragraphs(units, speaker_aware):
    """units: [{"t", "text", "speaker"}] in time order -> paragraphs."""
    paragraphs = []
    current = None
    previous_time = None
    for unit in units:
        text = unit["text"].strip()
        if not text:
            continue
        pause = unit["t"] - previous_time if previous_time is not None else 0.0
        start_new = (
            current is None
            or (speaker_aware and unit["speaker"] != current["speaker"])
            or (pause > PARAGRAPH_PAUSE and len(current["text"]) >= 120)
            or (len(current["text"]) >= PARAGRAPH_CHARS and SENTENCE_END.search(current["text"]))
            or len(current["text"]) >= PARAGRAPH_HARD_CHARS
        )
        if start_new:
            current = {"speaker": unit.get("speaker"), "start": unit["t"], "text": text, "units": [unit]}
            paragraphs.append(current)
        else:
            current["text"] += " " + text
            current["units"].append(unit)
        previous_time = unit["t"]
    if speaker_aware:
        # "Угу" / "Да" from someone else in the middle of a monologue would
        # split it into two paragraphs; join the monologue back together.
        merged = []
        for paragraph in paragraphs:
            if (len(merged) >= 2 and merged[-2]["speaker"] == paragraph["speaker"]
                    and _is_backchannel(merged[-1]["text"])):
                merged.pop()
                merged[-1]["text"] += " " + paragraph["text"]
                merged[-1]["units"] += paragraph["units"]
            else:
                merged.append(paragraph)
        paragraphs = merged
    return paragraphs


def _remarks(paragraphs, segments, russian):
    """Stage directions per paragraph, relative to each speaker's usual manner."""
    from tone import PROFANITY_RE, choose_remarks, paragraph_stats

    stats = [paragraph_stats([u["word"] for u in p["units"]], segments) for p in paragraphs]
    baselines = {}
    for speaker in {p["speaker"] for p in paragraphs}:
        own = [s for s, p in zip(stats, paragraphs) if p["speaker"] == speaker]
        dbs = [s["db"] for s in own if s["db"] is not None]
        rates = [s["rate"] for s in own if s["rate"]]
        baselines[speaker] = {"db": float(np.median(dbs)) if dbs else None, "rate": float(np.median(rates)) if rates else None}
    remarks = []
    for s, p in zip(stats, paragraphs):
        # Short replies have too little signal for a remark, unless they swear.
        if len(p["text"].split()) < 6:
            remarks.append(choose_remarks(s, baselines[p["speaker"]], p["text"], russian)[:1] if PROFANITY_RE.search(p["text"]) else [])
        else:
            remarks.append(choose_remarks(s, baselines[p["speaker"]], p["text"], russian))
    return remarks


def build_lines(words, speakers, structured, russian=True, segments=None):
    """Transcript text with a speaker label on every paragraph.

    With segments (carrying "tone"), labels get stage directions:
    "Спикер 2 (раздражённо, повысив голос): …".
    """
    units = [{"t": w["t"], "text": str(w["w"]), "speaker": s, "word": w} for w, s in zip(words, speakers)]
    paragraphs = _paragraphs(units, speaker_aware=True)
    remarks = _remarks(paragraphs, segments, russian) if segments is not None else [[] for _ in paragraphs]
    blocks = []
    for paragraph, remark in zip(paragraphs, remarks):
        label = speaker_label(paragraph["speaker"], russian)
        if remark:
            label += f" ({', '.join(remark)})"
        prefix = f"[{format_timestamp(paragraph['start'])}] " if structured else ""
        blocks.append(f"{prefix}{label}: {paragraph['text']}")
    return "\n\n".join(blocks) + "\n"


def build_plain_paragraphs(segments, structured):
    """Transcript without speakers, in paragraphs instead of one line per phrase."""
    units = [{"t": float(s.get("start", 0) or 0), "text": str(s.get("text", ""))} for s in segments]
    paragraphs = _paragraphs(units, speaker_aware=False)
    if structured:
        return "\n\n".join(f"[{format_timestamp(p['start'])}] {p['text']}" for p in paragraphs) + "\n"
    return "\n\n".join(p["text"] for p in paragraphs) + "\n"


def diarize_transcript(segments, embedding_sources, output_path, structured, num_speakers=None, russian=True, remarks=False):
    """Write the speaker-labelled transcript; return the speaker summary."""
    windows, embeddings = load_embeddings(embedding_sources)
    labels = cluster_speakers(embeddings, num_speakers)
    words = []
    for index, segment in enumerate(segments):
        for word in segment_words(segment):
            if not FILLER_RE.match(str(word["w"]).strip()):
                words.append({"t": word["t"], "w": word["w"], "seg": index})
    words.sort(key=lambda w: w["t"])
    speakers = assign_speakers(words, windows, labels)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(build_lines(words, speakers, structured, russian, segments if remarks else None))
    counts = {}
    for speaker in speakers:
        counts[speaker] = counts.get(speaker, 0) + 1
    total = max(1, len(speakers))
    return [
        {"id": number, "label": speaker_label(number, russian), "share": round(100 * counts[number] / total)}
        for number in sorted(counts)
    ]


LABEL_LINE = re.compile(r"^(\[[0-9:]+\] )?((?:Спикер|Speaker) (\d+))( \([^)\n]*\))?:", re.M)


def rename_speakers(raw_text, names):
    """Replace speaker labels at line starts with names ({number: name})."""
    def replace(match):
        name = names.get(int(match.group(3)))
        return f"{match.group(1) or ''}{name or match.group(2)}{match.group(4) or ''}:"
    return LABEL_LINE.sub(replace, raw_text)
