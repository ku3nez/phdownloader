"""Suggest real names for "Speaker N" labels from the conversation itself.

Every name occurrence in a speaker's line is classified with fixed patterns
and Russian morphology (pymorphy3):

- self: the speaker introduces themselves ("меня зовут Олег", "я — Олег");
- introduced: the speaker introduces a guest ("у нас в гостях Михаил Соколов");
- addressee: the speaker addresses someone by name; a Russian address is in
  the nominative and set off by punctuation ("Анна, скажите…", "Спасибо, Олег.");
- anything else (other cases, "мой брат Сергей") is a mention and ignored.

The role decides whose name it is: self -> this speaker, introduced -> the
next speaker, addressee -> the speaker being answered (or the one who answers
a question). Votes over the whole conversation give one name per speaker;
a name pointing equally to two speakers is dropped.
"""

import re

ROLE_WEIGHTS = {"self": 3, "introduced": 2, "addressee": 2}
LINE_RE = re.compile(r"^(?:\[([0-9:]+)\] )?((?:Спикер|Speaker) (\d+)): (.*)$")
TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)?|[^\sA-Za-zА-Яа-яЁё]")

SELF_BEFORE_RE = re.compile(r"(?:меня зовут|зовут меня|мо[её] имя|я\s*[—–-]|my name is|i am|i'm|this is)\s*$", re.I)
INTRO_BEFORE_RE = re.compile(
    r"(?:в гостях|в гостях у нас|у нас (?:сегодня )?(?:в гостях|в студии)|наш гость|наша гостья|гость(?:я)? (?:студии|программы)|"
    r"представляю(?: вам)?|познакомьтесь|знакомьтесь|встречайте|с нами (?:сегодня )?|joining us|our guest|please welcome|welcome)"
    r"[\w\s,—–-]{0,40}$", re.I)
GREETINGS = {"спасибо", "благодарю", "привет", "здравствуйте", "здравствуй", "добрый", "доброе", "пока", "thanks", "thank", "hi", "hello", "hey"}
RELATIONS = {
    "брат", "сестра", "мама", "папа", "мать", "отец", "сын", "дочь", "жена", "муж", "друг", "подруга", "коллега", "дед",
    "дедушка", "бабушка", "бабуля", "дядя", "тётя", "тетя", "начальник", "шеф", "директор", "сосед", "соседка", "учитель",
    "профессор", "доктор", "brother", "sister", "mother", "father", "friend", "colleague", "wife", "husband",
}
SECOND_PERSON = {
    "ты", "вы", "тебя", "вас", "тебе", "вам", "тобой", "вами", "твой", "твоя", "твоё", "твое", "твои", "твоего", "твоей",
    "ваш", "ваша", "ваше", "ваши", "вашего", "вашей", "you", "your", "yours",
}
FILLERS = {"а", "ну", "да", "нет", "вот", "ой", "э", "ага", "угу", "так", "и", "же", "ли", "оу", "ах", "эх", "oh", "yes", "no", "well"}
SENTENCE_END = {".", "!", "?", "…"}
EN_STOP = {"I", "The", "And", "But", "So", "Yes", "No", "Okay", "OK", "Well", "Oh", "Thanks", "Thank", "Hello", "Hi"}

_morph = None


def _analyzer():
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    return _morph


def parse_lines(raw_text):
    lines = []
    for line in raw_text.splitlines():
        match = LINE_RE.match(line.strip())
        if match:
            lines.append({"speaker": int(match.group(3)), "text": match.group(4)})
    return lines


def _russian_name_at(tokens, index):
    """(display name, is_nominative, end_index) if a Russian first name starts here."""
    word = tokens[index]
    if not word[:1].isupper() or not re.match(r"[А-ЯЁ]", word):
        return None
    # Only whole dictionary words: pymorphy3 otherwise guesses from parts,
    # e.g. "Антинейтрины" = prefix "анти" + a female name "Нейтрина".
    parses = [p for p in _analyzer().parse(word)
              if "Name" in p.tag and p.score >= 0.05 and len(p.methods_stack) == 1]
    if not parses:
        return None
    best = parses[0]
    nominative = any("nomn" in p.tag for p in parses if p.score >= 0.2) or "nomn" in best.tag
    first = best.inflect({"nomn"}) or best
    gender = "femn" if "femn" in best.tag else "masc"
    name = [first.word.capitalize()]
    end = index + 1
    # Optional patronymic and surname in the same case.
    while end < len(tokens) and len(name) < 3 and tokens[end][:1].isupper() and re.match(r"[А-ЯЁ]", tokens[end]):
        extra = [p for p in _analyzer().parse(tokens[end]) if ("Patr" in p.tag or "Surn" in p.tag) and p.score >= 0.1]
        if not extra:
            break
        # Keep the first name's gender: "Катя Калашникова", not "Калашников".
        extra.sort(key=lambda p: gender not in p.tag)
        form = extra[0].inflect({"nomn", gender}) or extra[0].inflect({"nomn"}) or extra[0]
        name.append(form.word.capitalize())
        end += 1
    return " ".join(name), nominative, end


def _english_name_at(tokens, index):
    word = tokens[index]
    if not re.fullmatch(r"[A-Z][a-z]{2,}", word) or word in EN_STOP:
        return None
    end = index + 1
    name = [word]
    if end < len(tokens) and re.fullmatch(r"[A-Z][a-z]{2,}", tokens[end]):
        name.append(tokens[end])
        end += 1
    return " ".join(name), True, end


def _addresses_listener(tokens, start, end):
    """The sentence around tokens[start:end] speaks to someone.

    A Russian address needs more than the bare name ("Бенедикт?", "Жанна, да,
    правильно" are answers, not addresses) and a question or a word aimed at
    the listener: ты/вы forms, or a verb in the 2nd person or imperative.
    """
    left = start
    while left > 0 and tokens[left - 1] not in SENTENCE_END:
        left -= 1
    right = end
    while right < len(tokens) and tokens[right] not in SENTENCE_END:
        right += 1
    words = [w.lower() for w in tokens[left:start] + tokens[end:right] if w[:1].isalpha()]
    content = [w for w in words if w not in FILLERS]
    if len(content) < 2:
        return False
    if right < len(tokens) and tokens[right] == "?":
        return True
    if any(w in SECOND_PERSON for w in words):
        return True
    for word in words:
        for parse in _analyzer().parse(word)[:2]:
            if "VERB" in parse.tag and ("2per" in parse.tag or "impr" in parse.tag) and parse.score >= 0.3:
                return True
    return False


def find_mentions(text):
    """[(name, role, nominative, words_after)] for every name occurrence in one line."""
    tokens = TOKEN_RE.findall(text)
    cyrillic = bool(re.search(r"[А-Яа-яЁё]", text))
    mentions = []
    index = 0
    while index < len(tokens):
        # In Russian speech Latin words are band, film or brand names
        # ("Смотри, Sleepknot, …"), so English names are looked for only in
        # English text.
        found = _russian_name_at(tokens, index) or (None if cyrillic else _english_name_at(tokens, index))
        if not found:
            index += 1
            continue
        name, nominative, end = found
        before = " ".join(tokens[max(0, index - 8):index])
        before = re.sub(r"\s+([,.!?—–-])", r"\1", before)
        prev_token = tokens[index - 1] if index > 0 else ""
        prev_word = next((t for t in reversed(tokens[:index]) if t[:1].isalpha()), "").lower()
        next_token = tokens[end] if end < len(tokens) else ""
        sentence_start = index == 0 or prev_token in {".", "!", "?", "…"}
        set_off_before = sentence_start or prev_token in {",", "—", "–", "-"}
        set_off_after = next_token in {",", ".", "!", "?", "…", ""}
        role = "mentioned"
        if nominative and SELF_BEFORE_RE.search(before):
            role = "self"
        elif nominative and INTRO_BEFORE_RE.search(before):
            role = "introduced"
        elif nominative and prev_word in GREETINGS and set_off_after:
            role = "addressee"
        elif (nominative and set_off_before and set_off_after and prev_word not in RELATIONS
              and _addresses_listener(tokens, index, end)):
            role = "addressee"
        mentions.append((name, role, nominative, sum(1 for t in tokens[end:] if t[:1].isalpha())))
        index = end
    return mentions


# A real greeting near the start of the reply ("Спасибо. Всем привет." counts);
# thanks alone only closes a remark and does not mark an introduction.
GREETING_RE = re.compile(
    r"здравствуй|привет|добр(?:ый|ое|ого) (?:день|вечер|утро|дня|вечера)|доброе утро|рада? (?:быть|видеть|приветствовать)|"
    r"\bhello\b|\bhi\b|\bhey\b|good (?:morning|evening|afternoon)|nice to (?:meet|be)",
    re.I)


def _answered_with_greeting(lines, index):
    """The next other speaker starts with a greeting: the name was an introduction."""
    speaker = lines[index]["speaker"]
    for j in range(index + 1, min(len(lines), index + 3)):
        if lines[j]["speaker"] != speaker:
            return bool(GREETING_RE.search(lines[j]["text"][:60]))
    return False


def _stem(name):
    word = name.split()[0].lower().replace("ё", "е")
    return word[:max(3, len(word) - 2)]


def _owner(lines, index, role):
    speaker = lines[index]["speaker"]
    if role == "self":
        return speaker
    following = next((lines[j]["speaker"] for j in range(index + 1, min(len(lines), index + 4)) if lines[j]["speaker"] != speaker), None)
    preceding = next((lines[j]["speaker"] for j in range(index - 1, max(-1, index - 4), -1) if lines[j]["speaker"] != speaker), None)
    if role == "introduced":
        return following
    if role == "addressee":
        if "?" in lines[index]["text"]:
            return following or preceding
        return preceding or following
    return None


def suggest_names(raw_text):
    """[{"id", "name", "evidence"}] for speakers whose name can be inferred."""
    lines = parse_lines(raw_text)
    votes, forms, evidence = {}, {}, {}
    for index, line in enumerate(lines):
        for name, role, nominative, words_after in find_mentions(line["text"]):
            # "Есть такие люди? — Дмитрий Сергеевич Горбунов." answered by
            # "Здравствуйте" introduces the next speaker; the name has to close
            # the line, or any name in a long reply would count.
            if role == "mentioned" and nominative and words_after <= 6 and _answered_with_greeting(lines, index):
                role = "introduced"
            if role not in ROLE_WEIGHTS:
                continue
            owner = _owner(lines, index, role)
            if owner is None:
                continue
            key = _stem(name)
            votes.setdefault(key, {}).setdefault(owner, 0)
            votes[key][owner] += ROLE_WEIGHTS[role]
            if len(name) > len(forms.get(key, "")):
                forms[key] = name
            evidence.setdefault((key, owner), line["text"][:200])
    best = {}
    for key, by_speaker in votes.items():
        ranked = sorted(by_speaker.items(), key=lambda item: -item[1])
        # With three or more people an address can be ambiguous; take the
        # name only if one speaker clearly collects most of its votes.
        if len(ranked) > 1 and ranked[0][1] < 1.5 * ranked[1][1]:
            continue
        speaker, score = ranked[0]
        if score > best.get(speaker, (None, 0))[1]:
            best[speaker] = (key, score)
    return [{"id": s, "name": forms[k], "evidence": evidence[(k, s)]} for s, (k, _) in sorted(best.items())]
