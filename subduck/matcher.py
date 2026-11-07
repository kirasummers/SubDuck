import re
from typing import List, Tuple
from subduck.models import SubtitleEntry, TriggerMatch, CensorConfig

def build_word_regex(word: str, strict: bool = True) -> re.Pattern:
    """
    Build a regex pattern for a trigger word.
    Strict mode enforces boundaries around alphanumeric characters,
    allowing hyphens, apostrophes, and punctuation (e.g. 'cancer' matches 'cancer-sign').
    """
    clean_word = word.strip()
    escaped = re.escape(clean_word)
    if strict:
        return re.compile(rf"(?i)(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])")
    else:
        return re.compile(rf"(?i){escaped}")

def censor_word_token(word: str, style: str) -> str:
    """Format the replacement string for a detected trigger word."""
    length = len(word)
    if style == "asterisks":
        return "*" * length
    elif style == "inner_asterisks":
        if length <= 2:
            return "*" * length
        return word[0] + ("*" * (length - 2)) + word[-1]
    elif style == "tag":
        return "[censored]"
    elif style == "block":
        return "█" * length
    elif style == "remove":
        return ""
    return "*" * length

def censor_text_line(text: str, trigger_words: List[str], style: str, strict: bool) -> Tuple[str, List[str]]:
    """
    Censor all occurrences of trigger words in a text line.
    Returns the censored string and a list of matched words.
    """
    matched = []
    result = text

    for w in trigger_words:
        w = w.strip()
        if not w:
            continue
        pattern = build_word_regex(w, strict)
        found = pattern.findall(result)
        if found:
            matched.extend(found)
            result = pattern.sub(lambda m: censor_word_token(m.group(0), style), result)

    # Clean up double spaces if words were removed
    if style == "remove":
        result = re.sub(r"\s+", " ", result).strip()

    return result, matched

def scan_subtitles_for_triggers(
    entries: List[SubtitleEntry],
    config: CensorConfig
) -> List[TriggerMatch]:
    """Scan all subtitle entries against the config's trigger words."""
    matches = []
    if not config.trigger_words:
        return matches

    for entry in entries:
        censored_text, matched_words = censor_text_line(
            entry.text,
            config.trigger_words,
            config.censor_style,
            config.strict_matching
        )
        if matched_words:
            matches.append(TriggerMatch(
                entry=entry,
                matched_words=list(set(matched_words)),
                censored_text=censored_text
            ))

    return matches

def calculate_merged_mute_intervals(
    matches: List[TriggerMatch],
    safety_before_ms: int = 200,
    safety_after_ms: int = 200
) -> List[Tuple[float, float]]:
    """
    Calculate and merge audio mute time windows (in seconds).
    Adds lead-in and lead-out safety buffers and merges overlapping windows.
    """
    if not matches:
        return []

    pad_before = max(0.0, safety_before_ms / 1000.0)
    pad_after = max(0.0, safety_after_ms / 1000.0)

    # Create raw interval windows
    raw_intervals = []
    for m in matches:
        start = max(0.0, m.entry.start_sec - pad_before)
        end = m.entry.end_sec + pad_after
        raw_intervals.append((start, end))

    # Sort by start time
    raw_intervals.sort(key=lambda x: x[0])

    # Merge overlapping or contiguous intervals
    merged = [raw_intervals[0]]
    for cur_start, cur_end in raw_intervals[1:]:
        prev_start, prev_end = merged[-1]
        if cur_start <= prev_end + 0.05:  # within 50ms is joined
            merged[-1] = (prev_start, max(prev_end, cur_end))
        else:
            merged.append((cur_start, cur_end))

    return merged
