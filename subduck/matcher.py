import re
from subduck.models import SubtitleEntry, TriggerMatch, CensorConfig


def build_word_regex(word: str, strict: bool = True) -> re.Pattern:
    escaped = re.escape(word.strip())
    if strict:
        return re.compile(rf"(?i)(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])")
    return re.compile(rf"(?i){escaped}")


def censor_word_token(word: str, style: str) -> str:
    n = len(word)
    if style == "inner_asterisks":
        return "*" * n if n <= 2 else word[0] + ("*" * (n - 2)) + word[-1]
    if style == "tag":
        return "[censored]"
    if style == "block":
        return "█" * n
    if style == "remove":
        return ""
    return "*" * n


def censor_text_line(text: str, trigger_words: list[str], style: str, strict: bool):
    matched = []
    result = text

    for raw_word in trigger_words:
        w = raw_word.strip()
        if not w:
            continue
        pat = build_word_regex(w, strict)
        hits = pat.findall(result)
        if hits:
            matched.extend(hits)
            result = pat.sub(lambda m: censor_word_token(m.group(0), style), result)

    if style == "remove":
        result = re.sub(r"\s+", " ", result).strip()

    return result, matched


def scan_subtitles_for_triggers(entries: list[SubtitleEntry], config: CensorConfig) -> list[TriggerMatch]:
    if not config.trigger_words:
        return []

    matches = []
    for entry in entries:
        censored_text, hits = censor_text_line(
            entry.text,
            config.trigger_words,
            config.censor_style,
            config.strict_matching
        )
        if hits:
            seen = set()
            deduped = []
            for h in hits:
                if h not in seen:
                    seen.add(h)
                    deduped.append(h)
            matches.append(TriggerMatch(
                entry=entry,
                matched_words=deduped,
                censored_text=censored_text
            ))

    return matches


def calculate_merged_mute_intervals(
    matches: list[TriggerMatch],
    safety_before_ms: int = 200,
    safety_after_ms: int = 200,
    strict: bool = True
) -> list[tuple[float, float]]:
    if not matches:
        return []

    pad_in = max(0.0, safety_before_ms / 1000.0)
    pad_out = max(0.0, safety_after_ms / 1000.0)

    windows = []
    for m in matches:
        word_hits = []
        if m.entry.word_timings:
            has_phrase = any(" " in mw.strip() for mw in m.matched_words)
            if not has_phrase:
                pats = [build_word_regex(mw, strict=strict) for mw in m.matched_words]
                for tok, w_start, w_end in m.entry.word_timings:
                    if any(p.search(tok) for p in pats):
                        word_hits.append((max(0.0, w_start - pad_in), w_end + pad_out))

        if word_hits:
            windows.extend(word_hits)
        else:
            windows.append((max(0.0, m.entry.start_sec - pad_in), m.entry.end_sec + pad_out))

    windows.sort(key=lambda x: x[0])

    merged = [windows[0]]
    for cur_s, cur_e in windows[1:]:
        prev_s, prev_e = merged[-1]
        if cur_s <= prev_e + 0.05:
            merged[-1] = (prev_s, max(prev_e, cur_e))
        else:
            merged.append((cur_s, cur_e))

    return merged
