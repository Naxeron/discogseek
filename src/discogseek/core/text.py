"""
Unified text processing, NLP, fuzzy matching, and Unicode normalization for discogseek.
"""

import re
import time
import unicodedata
import urllib.parse
from functools import lru_cache
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Set, Tuple, Optional, Any
from unidecode import unidecode

from discogseek.core.constants import (
    POLISH_DIACRITICS_TABLE,
    TITLE_AUDIO_EXTENSIONS,
    DIR_STOP_WORDS,
    GENERIC_OR_COMMON_WORDS,
)

# Japanese Zenkaku/Hankaku & Kana Transliteration Mappings
KATAKANA_TO_HIRAGANA: Dict[int, int] = {
    i: i - 0x60 for i in range(0x30A1, 0x30F7)
}

ZENKAKU_TO_HANKAKU: Dict[int, int] = {
    0x3000: 0x0020,  # Ideographic space -> ASCII space
    **{i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)},  # Fullwidth ASCII -> Halfwidth ASCII
}

KANJI_NUMERALS: Dict[str, str] = {
    "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
    "十": "10", "百": "100", "千": "1000",
}

ROMAN_NUMERALS_MAP: Dict[str, str] = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
    "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
    "xvi": "16", "xvii": "17", "xviii": "18", "xix": "19", "xx": "20",
}

UNAMBIGUOUS_ROMAN: Set[str] = {
    "ii", "iii", "iv", "vi", "vii", "viii", "ix",
    "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx"
}


def normalize_roman_numerals(text: str) -> str:
    """
    Translates Roman numerals (I through XX) in track and movement indicators to Arabic digits.
    Guards against mangling common words and pronouns (e.g. 'I', 'V', 'X').
    """
    if not text:
        return ""

    # 1. Indicator + Roman numeral (I through XX, case-insensitive)
    pat_indicator = re.compile(
        r"(\b(?:part|pt|act|movement|mov|mvt|vol|volume|chapter|suite|no|nr|track|scene|phase|section|book|canto|opus|op)\b\.?\s*)([ivx]{1,6})\b",
        re.IGNORECASE
    )
    result = pat_indicator.sub(
        lambda m: f"{m.group(1)}{ROMAN_NUMERALS_MAP.get(m.group(2).lower(), m.group(2))}",
        text
    )

    # 2. Standalone unambiguous Roman numerals (II through XX, excluding single letters 'I', 'V', 'X')
    pat_unambiguous = re.compile(r"\b([ivx]{2,6})\b", re.IGNORECASE)
    result = pat_unambiguous.sub(
        lambda m: ROMAN_NUMERALS_MAP.get(m.group(1).lower(), m.group(1))
        if m.group(1).lower() in UNAMBIGUOUS_ROMAN else m.group(1),
        result
    )

    # 3. Bracketed Roman numerals: (I), [I], {I}, (V), [V], (X)
    pat_bracketed = re.compile(r"([\(\[\{])\s*([ivx]{1,6})\s*([\)\]\}])", re.IGNORECASE)
    result = pat_bracketed.sub(
        lambda m: f"{m.group(1)}{ROMAN_NUMERALS_MAP.get(m.group(2).lower(), m.group(2))}{m.group(3)}"
        if m.group(2).lower() in ROMAN_NUMERALS_MAP else m.group(0),
        result
    )

    return result


def kanji_to_arabic(text: str) -> str:
    """Converts simple Kanji and full-width Japanese numerals to Arabic digits."""
    if not text:
        return ""
    result = text
    if "十二" in result:
        result = result.replace("十二", "12")
    if "十一" in result:
        result = result.replace("十一", "11")
    if "十" in result:
        result = re.sub(r"([1-9])十([1-9])", r"\g<1>\g<2>", result)
        result = re.sub(r"([1-9])十", r"\g<1>0", result)
        result = re.sub(r"十([1-9])", r"1\g<1>", result)
        result = result.replace("十", "10")
    for kanji, digit in KANJI_NUMERALS.items():
        result = result.replace(kanji, digit)
    return result


def _symbol_key(text: str) -> str:
    """Casefolded NFC symbols of a title that ASCII folding erases (★, ∞, emoji)."""
    chars = []
    for ch in unicodedata.normalize("NFC", text).casefold():
        category = unicodedata.category(ch)
        if category == "Cf" or 0xFE00 <= ord(ch) <= 0xFE0F or 0xE0100 <= ord(ch) <= 0xE01EF:
            continue  # zero-width joiners and emoji variation selectors
        chars.append(" " if ch.isspace() or category[0] == "P" else ch)
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _symbol_as_text(char: str) -> str:
    """A letter-like symbol's letter (circled 'Ⓢ' -> 'S'); a separator for decorative ones."""
    # Keep the letters of mixed forms too: '℃' -> ' C', '⒜' -> ' a ', '℅' -> 'c o'.
    return "".join(c if c.isalnum() else " " for c in unicodedata.normalize("NFKC", char))


@lru_cache(maxsize=65536)
def normalize_text(text: Optional[str]) -> str:
    """
    Applies comprehensive Unicode NFC normalization, Katakana/Hiragana unification,
    Kanji numeral conversion, Roman numeral conversion, punctuation stripping, and diacritics removal.
    '_' counts as a space and '&' as 'and'. Titles that ASCII folding erases entirely
    (★, ∞, emoji) normalize to their casefolded symbols so they remain comparable.
    """
    if not text:
        return ""

    raw = unicodedata.normalize('NFC', str(text)).strip()
    raw = raw.translate(ZENKAKU_TO_HANKAKU)
    raw = raw.translate(KATAKANA_TO_HIRAGANA)
    raw = raw.translate(POLISH_DIACRITICS_TABLE)
    raw = kanji_to_arabic(raw)
    raw = normalize_roman_numerals(raw)
    # 'Tried & True' and 'Tried and True' name the same song.
    raw = raw.replace("&", " and ")
    if any(char.isalnum() for char in raw):
        # Decorative symbols inside names vary between taggers ('sato❤shin', 'sato♥shin')
        # and ASCII folding spells only some out ('hearts'). Letter-like symbols such as
        # circled letters keep their letter; symbol-only titles use the fallback below.
        raw = "".join(_symbol_as_text(char) if unicodedata.category(char) == "So" else char for char in raw)
    ascii_norm = unidecode(raw).lower()
    # '_' is a regex word character, but peers use it in place of spaces.
    clean = re.sub(r"[^\w\s]|_", " ", ascii_norm)
    clean = re.sub(r"\s+", " ", clean).strip()
    if clean:
        return clean
    return _symbol_key(raw) or ascii_norm.strip()


@lru_cache(maxsize=65536)
def clean_tokens(text: Optional[str]) -> str:
    """Compresses normalized text into contiguous alphanumeric characters."""
    norm = normalize_text(text)
    return "".join(norm.split())


_SEARCH_KEPT_PUNCTUATION = frozenset("-.'_")


@lru_cache(maxsize=65536)
def clean_search_phrase(query: str) -> str:
    """
    Cleans punctuation and symbols from a search query for maximum index recall, preserving
    non-Latin scripts. Combining marks (Indic vowel signs, viramas, Thai tone marks) are kept,
    because dropping them splits words into fragments peers never match.
    """
    if not query:
        return ""
    q = unicodedata.normalize('NFC', str(query)).strip()
    q = q.translate(ZENKAKU_TO_HANKAKU)
    chars = []
    for ch in q:
        category = unicodedata.category(ch)
        if ch in _SEARCH_KEPT_PUNCTUATION:
            chars.append(ch)
        elif category[0] in "PS" or category == "Cf":
            chars.append(" ")
        else:
            chars.append(ch)
    q = re.sub(r"\s+", " ", "".join(chars)).strip()
    # A title made only of symbols ('David Bowie - ★') must not leave a dangling separator.
    q = re.sub(r"^(?:[-.']+\s+)+|(?:\s+[-.']+)+$", "", q)
    return q


def fold_latin_diacritics(text: str) -> str:
    """Folds Latin diacritics ('Zażółć' -> 'Zazolc') while leaving other scripts untouched."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFC", str(text)).translate(POLISH_DIACRITICS_TABLE)
    chars: List[str] = []
    for ch in unicodedata.normalize("NFD", folded):
        if unicodedata.category(ch) == "Mn" and chars and chars[-1].isascii() and chars[-1].isalpha():
            continue
        chars.append(ch)
    return unicodedata.normalize("NFC", "".join(chars))


def fold_search_phrase(query: str) -> str:
    """
    ASCII-folded Latin variant of clean_search_phrase for an extra query. Peers that store
    'Zazolc gesla jazn' are unreachable through the native spelling alone.
    """
    return clean_search_phrase(fold_latin_diacritics(query))


@lru_cache(maxsize=65536)
def _tokenize_words_cached(text: Optional[str]) -> Tuple[str, ...]:
    """Tokenizes text into a tuple of individual alphanumeric word tokens."""
    norm = normalize_text(text)
    if not norm:
        return ()
    return tuple(re.findall(r"[a-z0-9]+", norm))


def tokenize_words(text: Optional[str]) -> List[str]:
    """Extracts lowercase alphanumeric words with unidecode transliteration."""
    return list(_tokenize_words_cached(text))


def is_sublist(sub: List[str], full: List[str]) -> bool:
    """Checks if sub is an exact contiguous sub-sequence of full word tokens."""
    if not sub or not full or len(sub) > len(full):
        return False
    sub_len = len(sub)
    for i in range(len(full) - sub_len + 1):
        if full[i:i + sub_len] == sub:
            return True
    return False


@lru_cache(maxsize=65536)
def calculate_similarity(str1: str, str2: str) -> float:
    """Calculates SequenceMatcher ratio between two normalized strings."""
    if not str1 or not str2:
        return 0.0
    if str1 == str2:
        return 1.0
    return SequenceMatcher(None, str1, str2).ratio()


# A trailing ' - <descriptor>' segment describes the version of the title before it,
# e.g. 'Song - Alice Remix' or 'Song - 2011 Remaster'; it never replaces the title.
VERSION_DESCRIPTOR_RE = re.compile(
    r"^(?:"
    r"(?:.*?\s+)?(?:remix|rmx|re-mix|flip|bootleg|rework|refix|mashup|mash-up|re-?edit)(?:es|ed)?|"
    r"(?:(?:radio|club|extended|single|video|album|clean|short|vip)\s+)?edit|"
    r"(?:album|single|radio|extended|clean|explicit|instrumental|vocal|tv|short|full|mono|stereo)\s+ver(?:sion|\.)?|"
    r"(?:.*?\s+)?vip(?:\s*mix)?|"
    r"instrumental|inst|off\s*vocal|karaoke|backing\s*track|"
    r"acapella|a\s*cappella|vocal\s*version|"
    r"acoustic(?:\s*ver(?:sion)?)?|unplugged(?:\s*ver(?:sion)?)?|piano\s*ver(?:sion)?|"
    r"live(?:\s+at\s+.*|\s+in\s+.*|\s+version|\s*\d{4})?|"
    r"speed\s*up|sped\s*up|slowed|nightcore|daycore|chopped\s*and\s*screwed|"
    r"demo|alternat(?:e|ive)\s*(?:take|mix)|alt\s*take|alt\s*mix|rough\s*mix|"
    r"club\s*mix|extended\s*mix|extended\s*version|extended|radio\s*edit|dub\s*mix|dub|original\s*mix|album\s*version|"
    r"tv\s*size|game\s*size|"
    r"(?:(?:19|20)\d{2}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:version|edition))?"
    r"(?:\s+(?:19|20)\d{2})?(?:\s+(?:version|edition))?|"
    r"(?:\d+(?:st|nd|rd|th)\s+)?anniversary\s*(?:edition|version|remaster(?:ed)?)|"
    r"(?:deluxe|expanded|special)\s*(?:edition|version)|bonus\s*track|"
    r"explicit(?:\s+(?:version|lyrics))?|(?:clean|dirty)\s+version|"
    r"official\s+(?:music\s+|lyric\s+)?(?:audio|video|visuali[sz]er)|(?:music|lyric)\s+video|"
    r"(?:flac|mp3|wav|v[02]|\d{2,4}\s*k(?:bps)?)(?:[\s._-]+(?:flac|mp3|wav|v[02]|\d{2,4}\s*k(?:bps)?))*"
    r")$",
    re.IGNORECASE
)

_DASH_RE = re.compile(r"[\u2010-\u2015\u2212\uFF0D]")
_BRACKET_GROUP_RE = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]")
# ' - ' separators; one inside brackets never splits a title.
_SEPARATOR_RE = re.compile(r"\s+-+\s+")
_TRAILING_BRACKETS = r"((?:\s*[\(\[\{][^\(\)\[\]\{\}]*[\)\]\}])*)\s*$"
# 'Opening ~TV size~' or 'Memory of the Wind ～Legend～ [Extended Mix]'
_TRAILING_TILDE_GROUP_RE = re.compile(r"\s*[~～〜]\s*([^~～〜]+?)\s*[~～〜]" + _TRAILING_BRACKETS)
# 'Lose Control -Extended Mix-'
_TRAILING_DASH_GROUP_RE = re.compile(r"\s+-(?=\S)([^-]+?)(?<=\S)-" + _TRAILING_BRACKETS)
# 'Skelechairs Remix', 'Song VIP', 'Song Radio Edit', 'Song Remastered 2011' (no brackets)
_TRAILING_VERSION_WORD_RE = re.compile(
    r"^(.*?[^\W_].*?)\s+((?:radio|club|extended|dub|vip|original)\s+mix|"
    r"(?:(?:radio|club|extended|dub|vip)\s+)?(?:remix|rmx|edit)|vip|bonus\s+track|"
    r"(?:(?:19|20)\d{2}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:19|20)\d{2})?(?:\s+version)?)"
    + _TRAILING_BRACKETS,
    re.IGNORECASE,
)

# Peer filenames often end with format and bitrate tails: 'Rollercoaster.FLAC.932 kbps', 'Title  320bps'.
# A bare 'V2' is not one: 'Witches v2' and 'Estrella V2' name second versions.
_TECH_TAIL_TOKEN = r"(?:flac|mp3|wav|m4a|aac|ogg|v0|\d{2,4}\s*k?bps|\d{2,4}k)"
_TECH_TAIL_RE = re.compile(r"(?:[\s._-]+" + _TECH_TAIL_TOKEN + r")+\s*$", re.IGNORECASE)
_BITRATE_RE = re.compile(r"\d{2,4}\s*k?bps|\d{2,4}k\b", re.IGNORECASE)
# DJ libraries append the Camelot key and tempo: 'Biddies (Truth Is Light edit) - 7A - 96.4'.
_KEY_TEMPO_TAIL_RE = re.compile(r"\s+-\s+(?:1[0-2]|[1-9])[AB]\s+-\s+\d{2,3}(?:\.\d+)?(?:[_\s]*PN)*\s*$", re.IGNORECASE)

# Bracketed technical tags: '[FLAC]', '[889 kbps]', '(320kbps)', '[24-96]', '[16bit-44.1kHz]', '(MP3 96 kbps 44.1kHz)'.
_TECH_TOKEN_RE = re.compile(
    r"(?<![^\W_])(?:"
    r"flac|mp3|aac|alac|ogg|opus|wav|aiff?|m4a|wma|ape|dsd|lossless|lossy|vbr|cbr|abr|v[0-9]|"
    r"web(?:[\s-]?(?:rip|dl))?|cd(?:[\s-]?rip)?|vinyl(?:[\s-]?rip)?|rip|hi[\s-]?res|"
    r"\d{2,4}(?:[.,]\d+)?\s*k(?:bps|b/s|bit/s|bit)?|\d{2,4}\s*bps|kbps|"
    r"\d{2,3}(?:[.,]\d+)?\s*k?hz|(?:8|16|24|32)\s*-?\s*bits?|(?:16|24|32)\s*[-/]\s*\d{2,3}(?:[.,]\d+)?|"
    r"[1-9]\.[01]|stereo|mono|320|256|192|160|128"
    r")(?![^\W_])",
    re.IGNORECASE,
)
_MONO_STEREO_RE = re.compile(r"(?:mono|stereo)(?:\s+(?:version|mix))?", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?:^|[\s(\[])(?:www\.)?[a-z0-9][a-z0-9\-]*\.(?:com|net|org|ru|info|xyz|fm|de|fr|pl|io|me|tv|cc|us|biz|to|ws|co\.uk)\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

# Version & Remaster Patterns: whole bracket contents that never change which recording a title names.
REMASTER_OR_NOISE_PATTERNS = [
    re.compile(
        r"(?:(?:19|20)\d{2}\s*[-/]?\s*)?(?:digital(?:ly)?\s+)?re-?master(?:ed)?"
        r"(?:\s+(?:version|edition|in\s+(?:19|20)\d{2}))?(?:\s*[-/]?\s*(?:19|20)\d{2})?(?:\s+(?:version|edition))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:\d+(?:st|nd|rd|th)\s+)?anniversary(?:\s+(?:edition|version|remaster(?:ed)?))?|"
        r"(?:deluxe|expanded|special|limited|collector'?s)(?:\s+(?:edition|version))?|"
        r"(?:[\w'.-]+\s+){0,2}bonus(?:\s+track)?(?:\s+version)?",
        re.IGNORECASE,
    ),
    re.compile(r"original(?:\s+(?:mix|version))?|(?:album|lp|main)\s+(?:version|mix)", re.IGNORECASE),
    re.compile(r"(?:explicit|clean|dirty|edited|censored|uncensored)(?:\s+(?:version|lyrics|edit))?", re.IGNORECASE),
    re.compile(
        r"official\s+(?:music\s+|lyric\s+)?(?:audio|video|visuali[sz]er)|(?:music|lyric)\s+video|"
        r"lyrics?|visuali[sz]er|mv|pv|hq|hd|free\s+download|out\s+now",
        re.IGNORECASE,
    ),
    re.compile(r"[\d.]+\s*bpm", re.IGNORECASE),
]

# Uploader notes that only a bracket makes a note: '(Commission Song)', '(Remix pack in Description)'.
# Unbracketed, 'Artist - Commission' is a song called 'Commission'.
BRACKETED_NOTE_PATTERNS = [
    re.compile(
        r"commission(?:ed)?(?:\s+(?:song|work|track))?|"
        r"(?:(?:remix|sample|stem)s?\s+(?:pack|stem)s?|stems?|free\s+(?:dl|download)|download|dl)"
        r"\s+(?:in|on)\s+(?:the\s+)?description",
        re.IGNORECASE,
    ),
]

FEATURE_PATTERNS = [
    re.compile(r"[\(\[\{]\s*(?:feat\b\.?|ft\b\.?|featuring\b|with\b)\s*([^\(\)\[\]\{\}]+)[\)\]\}]", re.IGNORECASE),
    # Unbracketed credits end at a comma, a bracket, or a ' - ' separator ('Song ft. Jay-Z' keeps 'Jay-Z').
    re.compile(r"\b(?:feat\b\.?|ft\b\.?|featuring\b)\s*((?:(?!\s+-+\s)[^,\(\)\[\]\{\}])+)", re.IGNORECASE),
]

# A word ending in a remix keyword: 'Lone Remix', 'Uziflip', 'RINEDIT', 'Remixed'.
# Not 'Edition', 'Edith', 'Credits', or 'Edited' (a clean version).
_REMIX_WORD = (
    r"\b(?!\w*credits?\b)(?!edited\b)\w*?"
    r"(?:remix|rmx|re-mix|flip|bootleg|rework|refix|mashup|mash-up|edit)(?:x|ed|es|s)?\b"
)

# Ordered (bracket-content pattern, version type); the first type found anywhere wins.
VERSION_PATTERNS = [
    (re.compile(_REMIX_WORD, re.IGNORECASE), "remix"),
    (re.compile(r"\bvip\b", re.IGNORECASE), "vip"),
    (re.compile(r"\b(?:instrumental|inst|instr|off\s*vocal|karaoke|backing\s+track)\b", re.IGNORECASE), "instrumental"),
    (re.compile(r"\b(?:acapella|acappella|a\s*cappella|a\s*capella|vocal\s+version|vocals\s+only)\b", re.IGNORECASE), "acapella"),
    (re.compile(r"\b(?:acoustic|unplugged)\b|\bpiano\s+ver(?:sion\b|\b|\.)", re.IGNORECASE), "acoustic"),
    (re.compile(r"\blive\b", re.IGNORECASE), "live"),
    (re.compile(
        r"\b(?:sped\s*up|speed\s*up|slowed|nightcore|daycore|chopped\s*(?:and|&|n|'n')\s*screwed)\b", re.IGNORECASE,
    ), "speed"),
    (re.compile(
        r"\b(?:demos?|alternat(?:e|ive)\s+(?:take|mix|version)|alt\.?\s+(?:take|mix)|rough\s+mix|outtake)\b",
        re.IGNORECASE,
    ), "demo"),
    (re.compile(
        r"\b(?:club\s+mix|extended(?:\s+(?:mix|version))?|radio\s+(?:mix|version)|dub\s+mix|dubb?|"
        r"tv\s+(?:size|ver(?:sion)?)|short\s+ver(?:sion)?|game\s+size)\b",
        re.IGNORECASE,
    ), "mix_edit"),
    # '(Jimpster mix)' names a remixer exactly like '(Jimpster Remix)'.
    (re.compile(r"^\s*(?!(?:original|album|main|lp)\s+mix\s*$)[^\W_].*?\s+mix(?:\s+version)?\s*$", re.IGNORECASE), "remix"),
]
# An unbracketed ' - X Remix' inside a title.
_DASH_REMIX_RE = re.compile(
    r"(?:^|\s)-+\s*([^\-\(\)\[\]\{\}]*?" + _REMIX_WORD + r"[^\-\(\)\[\]\{\}]*)", re.IGNORECASE,
)


def _bracket_content(text: str) -> str:
    return text.replace("_", " ").strip()


def _is_tech_tag(content: str) -> bool:
    """True for bracket contents made only of format, bitrate, and sample-rate tags."""
    content = content.strip()
    if not content or _MONO_STEREO_RE.fullmatch(content):
        return False
    return not re.search(r"[^\W_]", _TECH_TOKEN_RE.sub(" ", content))


@lru_cache(maxsize=65536)
def _is_noise_content(content: str, opener: str = "(") -> bool:
    """
    Bracket contents that never change which recording a title names. opener '' marks an
    unbracketed ' - ' segment, which must be noise as a whole ('Title [site.net]' is a title).
    """
    content = _bracket_content(content)
    if not content:
        return False
    if any(pattern.fullmatch(content) for pattern in REMASTER_OR_NOISE_PATTERNS):
        return True
    if opener and any(pattern.fullmatch(content) for pattern in BRACKETED_NOTE_PATTERNS):
        return True
    if opener == "[" and re.fullmatch(r"[\d.]+", content):
        return True  # '[2]', '[128.5]' bpm or index tags
    # '[V2]' is a LAME preset, but '(v2)' and a ' - v2' segment name a second version.
    if opener != "[" and re.fullmatch(r"v\s*[1-9]", content, re.IGNORECASE):
        return False
    if _is_tech_tag(content):
        return True
    domain = _DOMAIN_RE.search(content)
    return bool(domain) and (bool(opener) or not _has_title_content(_DOMAIN_RE.sub(" ", content)))


@lru_cache(maxsize=65536)
def _is_descriptor(part: str) -> bool:
    """A ' - ' segment that describes the version or quality of the title before it."""
    part = part.strip()
    return bool(part) and bool(VERSION_DESCRIPTOR_RE.match(part) or _is_noise_content(part, ""))


def _has_title_content(text: str) -> bool:
    """True when text has letters, digits, or symbols outside brackets."""
    outside = _BRACKET_GROUP_RE.sub(" ", text)
    return any(unicodedata.category(ch)[0] in "LNS" for ch in outside)


def _split_outside_brackets(text: str) -> List[str]:
    """Splits on ' - ' separators that are not inside (), [] or {}."""
    parts = []
    start = 0
    for match in _SEPARATOR_RE.finditer(text):
        before = text[:match.start()]
        depth = sum(before.count(ch) for ch in "([{") - sum(before.count(ch) for ch in ")]}")
        if depth > 0:
            continue
        parts.append(text[start:match.start()])
        start = match.end()
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _fold_descriptors(parts: List[str]) -> List[str]:
    """['Song', 'Alice Remix'] -> ['Song (Alice Remix)']; a pure number never becomes the title."""
    parts = list(parts)
    while len(parts) >= 2 and _is_descriptor(parts[-1]):
        descriptor = parts.pop()
        title = parts[-1]
        if title.isdigit():
            parts[-1] = descriptor
            break
        parts[-1] = f"{title} ({descriptor})"
    return parts


def _bracket_trailing_groups(text: str) -> str:
    """'Opening ~TV size~' -> 'Opening (TV size)'; 'Title -Extended Mix-' -> 'Title (Extended Mix)'."""
    match = _TRAILING_TILDE_GROUP_RE.search(text)
    if match and _has_title_content(text[:match.start()]):
        text = f"{text[:match.start()]} ({match.group(1).strip()}){match.group(2)}"
    match = _TRAILING_DASH_GROUP_RE.search(text)
    if match and _has_title_content(text[:match.start()]) and _is_descriptor(match.group(1)):
        text = f"{text[:match.start()]} ({match.group(1).strip()}){match.group(2)}"
    return text


@lru_cache(maxsize=65536)
def _canonical_title(title: str) -> str:
    """
    Title-mode cleanup for MusicBrainz and tag titles. Never strips track numbers, artist
    prefixes, or extensions and never splits at ' - '; only version tails become brackets.
    """
    text = unicodedata.normalize("NFC", str(title or "")).strip()
    text = _DASH_RE.sub("-", text)
    text = re.sub(r"\s+", " ", text)
    text = _bracket_trailing_groups(text)
    parts = _split_outside_brackets(text)
    if len(parts) >= 2 and _is_descriptor(parts[-1]):
        text = " - ".join(_fold_descriptors(parts))
    return text.strip()


def _strip_audio_extension(text: str) -> str:
    match = re.search(r"\.([A-Za-z0-9]{2,4})$", text)
    if match and f".{match.group(1).lower()}" in TITLE_AUDIO_EXTENSIONS and match.start() > 0:
        return text[:match.start()].rstrip()
    return text


# Leading position tokens of a filename, tried in order; only one is stripped
# (plus an unambiguous second one such as '1 - 03.' or '05 2-05').
_TRACK_TOKEN_RULES = (
    (re.compile(r"^[\(\[]\s*(\d{1,3})\s*[\)\]]()\s*(?:[-._]+\s*)?(?=\S)"), "bracket"),
    (re.compile(r"^(\d{1,2}[-._]\d{1,3})(\s*[-._]+\s*|\s+)(?=\S)"), "disc_track"),
    (re.compile(r"^([A-Ha-h]\d{1,2})(\s*[-._]+\s*|\s+)(?=\S)"), "vinyl"),
    (re.compile(r"^(\d{1,4})(\s*[-._]+\s*)(?=\S)"), "number_sep"),
    (re.compile(r"^(\d{1,3}|0\d{3})(\s+)(?=\S)"), "number_space"),
)
_SECOND_TOKEN_RULES = (
    (re.compile(r"^(\d{1,2}[-.]\d{2,3})(\s*[-._]+\s*|\s+)(?=\S)"), "disc_track"),
    (re.compile(r"^(0\d{1,2})(\s*[-._]+\s*|\s+)(?=\S)"), "number_sep"),
)


def _strip_track_token(
    text: str, second: bool = False, keep_ambiguous: bool = False,
) -> Tuple[str, Optional[str], bool]:
    """
    Strips one leading track token. Returns (rest, kind, ambiguous); ambiguous marks an
    unpadded bare number ('2 Is 8', '21 Guns') that may belong to the title. kind is
    'number_space_long' for an ambiguous number of two or more digits.
    """
    for pattern, kind in (_SECOND_TOKEN_RULES if second else _TRACK_TOKEN_RULES):
        match = pattern.match(text)
        if not match:
            continue
        token, separator = match.group(1), match.group(2) or ""
        rest = text[match.end():]
        digits = token.lstrip("ABCDEFGHabcdefgh")
        padded = len(digits) > 1 and digits.startswith("0")
        spaced = separator != separator.replace(" ", "")
        punctuated = spaced and bool(separator.strip())
        # '5.23', '5-23' and '15_4' are titles, not a track number followed by a title.
        if kind in ("number_sep", "vinyl") and not spaced and rest[:1].isdigit():
            continue
        # '5 23 (Lone Remix)', '7 39' keep both numbers; '05 2 Is 8' and '23 22 8' are tracks 5 and 23.
        if kind == "number_space" and rest[:1].isdigit() and len(token) == 1:
            continue
        # '9 (Autechre remix)' and '5-23 (Lone Remix)' have no title without the number.
        if not _has_title_content(rest) and not (padded or punctuated or kind == "bracket"):
            continue
        ambiguous = kind == "number_space" and not padded
        if ambiguous and keep_ambiguous:
            return text, None, True
        if ambiguous and len(token) > 1:
            kind = "number_space_long"
        return rest, kind, ambiguous
    return text, None, False


_SCENE_TOKEN_RE = re.compile(r"^(?:\d{1,2}[-._]\d{1,3}|[A-Ha-h]\d{1,2}|\d{1,4})[-_.]+(?=[^\W_]|[\(\[])")
_SCENE_GROUP_RE = re.compile(r"^(?:[0-9a-f]{8}|[a-z0-9]{2,6})$")


def _scene_parts(stem: str) -> Optional[Tuple[List[str], Optional[str]]]:
    """
    Splits scene names such as '05-lone-big_world', '04-lone--visited_by_astronauts-oma',
    or 'Gold_Panda-You' into ([artist, title...], group). None for other filenames.
    """
    if re.search(r"\s", stem) or "-" not in stem:
        return None
    token = _SCENE_TOKEN_RE.match(stem)
    rest = stem[token.end():] if token else stem
    segments = [re.sub(r"\s+", " ", seg.replace("_", " ")).strip() for seg in re.split(r"-+", rest)]
    segments = [seg for seg in segments if seg]
    if not segments:
        return None
    # 'Re-Up' or '03-Re-Up' is a hyphenated title; scene names use '_' or lowercase words.
    if "_" not in stem and len(segments) < 3 and not (token and stem.islower()):
        return None
    group = None
    # '01-artist-song-remix' ends in a version, not a group tag.
    if len(segments) >= 3 and _SCENE_GROUP_RE.match(segments[-1]) and not _is_descriptor(segments[-1]):
        group = segments.pop()
    elif len(segments) == 2 and re.fullmatch(r"[0-9a-f]{8}", segments[-1]):
        group = segments.pop()
    return segments, group


_BARE_DESCRIPTOR_RE = re.compile(
    r"(?:\s*\b(?:remix|rmx|re-?edit|edit|mix|version|ver|extended|radio|club|dub|vip|original|"
    r"instrumental|inst|acoustic|live|demo|album|single|clean|explicit|tv|size|short)\b\.?)+\s*",
    re.IGNORECASE,
)


def _fold_scene_segments(segments: List[str]) -> List[str]:
    """
    Folds version segments into the title, never into the leading artist:
    ['artist', 'song remix'] stays, ['artist', 'song', 'remix'] -> ['artist', 'song (remix)'].
    Without an artist ('01-song-remix') a lone version segment folds into the first one.
    """
    if len(segments) >= 2:
        titles = _fold_descriptors(segments[1:])
        if not (len(titles) == 1 and (_BARE_DESCRIPTOR_RE.fullmatch(titles[0]) or _is_noise_content(titles[0], ""))):
            return [segments[0]] + titles
    return _fold_descriptors(segments)


def _title_part_variants(parts: List[str], scene: bool, alternatives: bool = False) -> List[str]:
    """Display titles from [artist?, ..., title] parts, the most likely first."""
    if not parts:
        return []
    if scene and len(parts) >= 2:
        variants = [" - ".join(parts[1:])]
        variants += [" - ".join(parts[i:]) for i in range(2, len(parts))]
    else:
        variants = [parts[-1]]
        variants += [" - ".join(parts[i:]) for i in range(len(parts) - 2, 0, -1)]
    variants.append(" - ".join(parts))
    if not alternatives:
        return variants
    # An alternative reading never makes a lone 'Edit' or '2012 Remaster' tag the title.
    return [v for v in variants if not (_BARE_DESCRIPTOR_RE.fullmatch(v) or _is_noise_content(v, ""))]


@lru_cache(maxsize=65536)
def _filename_title_variants(
    stem: str, keep_ambiguous: bool = False, primary_only: bool = False,
) -> Tuple[Tuple[str, ...], bool]:
    """
    Title candidates of a filename stem (extension already removed), the primary first,
    and whether an ambiguous leading number was stripped.
    """
    text = _DASH_RE.sub("-", unicodedata.normalize("NFC", stem).strip())
    scene = _scene_parts(text)
    if scene is not None:
        segments, group = scene
        variants = _title_part_variants(_fold_scene_segments(segments), scene=True)
        if primary_only:
            return (_canonical_title(variants[0]),), False
        variants += _title_part_variants(segments, scene=True, alternatives=True)
        if group:
            variants += _title_part_variants(segments + [group], scene=True, alternatives=True)
        return tuple(_canonical_title(v) for v in variants), False

    text = re.sub(r"\s+_\s+|_+-_+", " - ", text)
    text = re.sub(r"\s+", " ", text.replace("_", " ")).strip()
    tail = _TECH_TAIL_RE.search(text)
    if tail and tail.start() > 0 and (_BITRATE_RE.search(tail.group(0)) or not tail.group(0).startswith(".")):
        text = text[:tail.start()].rstrip()
    text = _KEY_TEMPO_TAIL_RE.sub("", text)
    text = _bracket_trailing_groups(text)
    # Remaining tildes and wave dashes separate artist and title like ' - '.
    text = re.sub(r"\s*[~～〜]\s*", " - ", text).strip(" -")
    text = re.sub(r"-\[\d+\]-", " - ", text)

    rest, kind, ambiguous = _strip_track_token(text, keep_ambiguous=keep_ambiguous)
    first_rest = rest
    repeated_rest = ""
    if kind:
        rest, _, _ = _strip_track_token(rest, second=True)
        # '13 13. $SSTFNMZ', '01 1. More Movement': the same position written twice. The
        # reading without it is an alternative; a title may itself begin with '1. '.
        lead = re.search(r"(\d+)\D*$", text[:len(text) - len(first_rest)])
        repeat = re.match(r"(\d{1,3})\s*[.)]\s*(?=\S)|(\d{1,3})\s+-\s+(?=\S)", rest)
        if (rest == first_rest and lead and repeat
                and int(repeat.group(1) or repeat.group(2)) == int(lead.group(1))
                and _has_title_content(rest[repeat.end():])):
            repeated_rest = rest[repeat.end():]

    parts = _split_outside_brackets(rest)
    # 'Artist - Album - 03 - Title': inner position segments are not titles.
    parts = [p for i, p in enumerate(parts) if i == len(parts) - 1 or not re.fullmatch(
        r"(?:\d{1,2}[-.])?\d{1,4}|[A-Ha-h]\d{1,2}", p)]
    unstripped = list(parts)
    if len(parts) >= 2:
        # 'Lone - 05 Big World', 'Artist - Album - 14 Home' and 'Work - 11. Movement': a number
        # after the artist is the position. After a leading position only '11. ' qualifies.
        last, last_kind, last_ambiguous = _strip_track_token(parts[-1])
        if last_kind and (
            last_kind == "number_sep" if kind else (not last_ambiguous or last_kind == "number_space_long")
        ):
            parts[-1] = last
    # 'Machinedrum - Gunshotta Lone Remix' may be a title by Machinedrum, not a remix of
    # 'Machinedrum', so the unfolded parts remain as variants.
    variants = _title_part_variants(_fold_descriptors(parts), scene=False)
    if primary_only:
        return (_canonical_title(variants[0]),) if variants else (), ambiguous
    variants += _title_part_variants(parts, scene=False, alternatives=True)
    if unstripped != parts:
        variants += _title_part_variants(_fold_descriptors(unstripped), scene=False, alternatives=True)
    # A zero-padded number may begin the title: '05 - 007 (Shanty Town)', '01 - 00 00',
    # '007 (Shanty Town)'. Keep the readings without the second token and without a bare
    # leading number as alternatives.
    for kept in (first_rest if first_rest != rest else "", text if kind == "number_space" else "", repeated_rest):
        if kept:
            variants += _title_part_variants(
                _fold_descriptors(_split_outside_brackets(kept)), scene=False, alternatives=True,
            )
    return tuple(_canonical_title(v) for v in variants if v.strip()), ambiguous


def _dedupe_titles(titles: Iterable[str]) -> Tuple[str, ...]:
    seen: Set[str] = set()
    result = []
    for title in titles:
        title = title.strip()
        key = normalize_text(title)
        if title and key and key not in seen:
            seen.add(key)
            result.append(title)
    return tuple(result)


@lru_cache(maxsize=65536)
def title_variants(title: str, is_filename: bool = True) -> Tuple[str, ...]:
    """
    Display title candidates for comparing a filename or title with another title, the most
    likely first. Feed each to parse_track_title_structure(variant, is_filename=False).

    Filenames yield the title after the artist plus every ' - '-joined suffix
    ('Interlude - Night Drive' and 'Night Drive'), the title with an ambiguous leading
    number kept ('2 Is 8'), scene splits with and without the group tag, and the title
    without a second extension ('YASTM.m4a.mp3' -> 'YASTM.m4a', 'YASTM'). Titles yield
    themselves plus the title without an extension-like ending.
    """
    raw = unicodedata.normalize("NFC", str(title or "")).strip()
    if not raw:
        return ()
    if not is_filename:
        canonical = _canonical_title(raw)
        return _dedupe_titles([canonical, _canonical_title(_strip_audio_extension(canonical))])
    stem = _strip_audio_extension(raw)
    candidates, ambiguous = _filename_title_variants(stem)
    candidates = list(candidates)
    if ambiguous:
        candidates += _filename_title_variants(stem, keep_ambiguous=True)[0]
    inner = _strip_audio_extension(stem)
    if inner != stem:
        candidates += _filename_title_variants(inner)[0]
    return _dedupe_titles(candidates)


@lru_cache(maxsize=65536)
def strip_track_number_and_artist(filename_no_ext: str) -> str:
    """
    Cleans a filename to extract the track title.
    e.g. '01 すてらべえ - Ultra Cutie Gangsta' -> 'Ultra Cutie Gangsta'
    e.g. '2-11 Stellabee - Enemy' -> 'Enemy'
    e.g. 'Aphex Twin - Syro - 03 - produk 29' -> 'produk 29'
    e.g. 'Aphex Twin (On)-[WAP 39CD]-[01]-On' -> 'On'
    e.g. '05-lone-big_world' -> 'big world'; '05 2 Is 8' -> '2 Is 8'
    """
    raw = unicodedata.normalize('NFC', str(filename_no_ext or "")).strip()
    stem = _strip_audio_extension(raw)
    candidates = _filename_title_variants(stem, primary_only=True)[0] if stem else ()
    return candidates[0] if candidates and candidates[0].strip() else stem


def _remove_brackets(text: str, should_remove) -> str:
    """Removes bracket groups whose (content, opener) satisfy should_remove, innermost first."""
    def replace(match: "re.Match[str]") -> str:
        return " " if should_remove(match.group(1), match.group(0)[0]) else match.group(0)
    return _BRACKET_GROUP_RE.sub(replace, text)


def _version_of(content: str) -> Optional[str]:
    content = _bracket_content(content)
    for pattern, vtype in VERSION_PATTERNS:
        if pattern.search(content):
            return vtype
    return None


def _structure(cleaned: str) -> Dict[str, Any]:
    """Splits a cleaned title into base, version, and feature credits."""
    raw = cleaned
    # 'Skelechairs Remix' -> 'Skelechairs (Remix)'
    trailing = _TRAILING_VERSION_WORD_RE.match(cleaned)
    if trailing and _has_title_content(trailing.group(1)):
        cleaned = f"{trailing.group(1)} ({trailing.group(2)}){trailing.group(3)}"

    # 1. Feature credits come out first: '(feat. Clive)' is not live, '(feat. Edith)' not an edit.
    features: List[str] = []

    def take_feature(match: "re.Match[str]") -> str:
        f_text = match.group(1).strip().rstrip(")]}")
        norm_f = normalize_text(f_text)
        if norm_f and norm_f not in features:
            features.append(norm_f)
        return " "

    without_features = cleaned
    for pat in FEATURE_PATTERNS:
        without_features = pat.sub(take_feature, without_features)

    # 2. Noise tags: remasters, editions, explicit markers, format and bitrate tags.
    without_noise = _remove_brackets(without_features, lambda content, opener: _is_noise_content(content, opener))
    # A bracketed year at either end ('Title (2011)', '(2025) Title') dates a file, not a recording.
    year = r"[\(\[\{]\s*" + _YEAR_RE.pattern + r"\s*[\)\]\}]"
    without_noise = re.sub(r"\s*" + year + r"\s*$", " ", without_noise)
    without_noise = re.sub(r"^\s*" + year + r"\s*", " ", without_noise)

    # 3. Version: the first type in VERSION_PATTERNS order found in any bracket.
    v_type = None
    v_text = None
    groups = [m.group(1) for m in _BRACKET_GROUP_RE.finditer(without_noise)]
    for pattern, vtype in VERSION_PATTERNS:
        for content in groups:
            if pattern.search(_bracket_content(content)):
                v_type, v_text = vtype, normalize_text(content)
                break
        if v_type:
            break
        if vtype == "remix" and pattern is VERSION_PATTERNS[0][0]:
            dash = _DASH_REMIX_RE.search(_BRACKET_GROUP_RE.sub(" ", without_noise))
            if dash:
                v_type, v_text = "remix", normalize_text(dash.group(1))
                break

    def strip_versions(text: str) -> str:
        text = _remove_brackets(text, lambda content, opener: _version_of(content) is not None)
        return _DASH_REMIX_RE.sub(" ", text)

    # 4. Base title; noise or a version is kept when nothing else would remain ('Lyrics', '(Remix)').
    base_str = strip_versions(without_noise)
    for fallback in (lambda: strip_versions(without_features), lambda: strip_versions(cleaned), lambda: cleaned):
        if normalize_text(base_str):
            break
        base_str = fallback()
    base_str = re.sub(r"\s+", " ", base_str).strip(" -")

    return {
        "raw": raw,
        "base_title": base_str,
        "base_norm": normalize_text(base_str),
        "version_type": v_type,
        "version_text": v_text,
        "features": features
    }


@lru_cache(maxsize=65536)
def parse_track_title_structure(title: str, is_filename: bool = True) -> Dict[str, Any]:
    """
    Parses a track title into structured components:
    - Base normalized title (with noise, remasters, format tags, and features stripped)
    - Base display title
    - Version type and normalized descriptor text (e.g. remix, instrumental, live)
    - Featured artist list

    is_filename=True treats the text as a peer filename: the audio extension, track number,
    and artist prefix are stripped and the title after the last ' - ' is used.
    is_filename=False is for MusicBrainz and tag titles, which keep leading numbers
    ('2 Is 8'), extension-like endings ('YASTM.m4a'), and ' - ' parts ('Mouth of God - Intro').
    """
    raw = unicodedata.normalize('NFC', str(title or "")).strip()
    if not raw:
        return {
            "raw": "",
            "base_title": "",
            "base_norm": "",
            "version_type": None,
            "version_text": None,
            "features": []
        }

    if is_filename:
        cleaned = strip_track_number_and_artist(raw)
    else:
        cleaned = _canonical_title(raw)
    parsed = _structure(cleaned or raw)
    parsed["raw"] = raw
    return parsed


def are_versions_compatible(
    v1_type: Optional[str],
    v1_text: Optional[str],
    v2_type: Optional[str],
    v2_text: Optional[str]
) -> bool:
    """
    Determines if two track version modifiers are musically compatible.
    Supports flexible matching for instrumental, acoustic, live, demo, and remix variations.
    '(Jimpster mix)' and '(Jimpster Remix)' are the same remix family.
    """
    if v1_type is None and v2_type is None:
        return True
    if (v1_type is None) != (v2_type is None):
        return False
    if v1_type != v2_type:
        return False

    if not v1_text or not v2_text:
        return True

    # Generic types where variations represent equivalent musical content
    if v1_type in ("instrumental", "acapella"):
        return True

    if v1_type in ("remix", "mix_edit", "vip"):
        c1 = re.sub(r"\b(remix|rmx|re-mix|flip|edit|bootleg|version|mix)\b", "", v1_text).strip()
        c2 = re.sub(r"\b(remix|rmx|re-mix|flip|edit|bootleg|version|mix)\b", "", v2_text).strip()
        if c1 and c2:
            sim = calculate_similarity(c1, c2)
            return (sim >= 0.75 or c1 in c2 or c2 in c1)
        sim = calculate_similarity(v1_text, v2_text)
        return (sim >= 0.75 or v1_text in v2_text or v2_text in v1_text)

    if v1_type in ("acoustic", "live", "demo"):
        descriptor_words = r"\b(version|take|mix|live|acoustic|demo|rough|alt|alternate|alternative|unplugged|piano)\b"
        c1 = re.sub(descriptor_words, "", v1_text).strip()
        c2 = re.sub(descriptor_words, "", v2_text).strip()
        if not c1 or not c2:
            return True
        if c1 == c2 or c1 in c2 or c2 in c1:
            return True
        return calculate_similarity(c1, c2) >= 0.75

    if v1_text == v2_text or v1_text in v2_text or v2_text in v1_text:
        return True
    return calculate_similarity(v1_text, v2_text) >= 0.75


def _title_numbers(norm: str) -> Set[int]:
    return {int(token) for token in re.findall(r"\b\d+\b", norm)}


# A word MusicBrainz printed with masked letters: 'fuXX' (letters, then an 'XX' run) or
# 'f**k' (asterisks between letters). A trailing 'star*' is decoration, not a mask.
_MASKED_WORD_RE = re.compile(r"^(?:([a-z]+)(X{2,})([a-z]*)|([a-z]+)(\*+)([a-z]+))$")
# A masked title is never exact: outside its release's folder it needs its position too.
_MASKED_TITLE_SCORE = 0.95


@lru_cache(maxsize=4096)
def _masked_title_pattern(expected_title: str) -> Optional["re.Pattern[str]"]:
    """
    'my fuXX Sound' as a pattern for normalized titles in which each masked letter is any
    letter ('my fuck sound'), or None when the title masks no word. An all-'X' word
    ('[XXXXXX]') is not a mask.
    """
    words = unicodedata.normalize("NFC", str(expected_title or "")).split()
    parts, masked = [], False
    for word in words:
        match = _MASKED_WORD_RE.match(word.strip(".,!?\"'"))
        if match:
            before, mask, after = (match.group(1, 2, 3) if match.group(1) else match.group(4, 5, 6))
            masked = True
            parts.append(re.escape(before) + f"[a-z]{{{len(mask)}}}" + re.escape((after or "").lower()))
        else:
            norm = normalize_text(word)
            if norm:
                parts.append(r"\s+".join(re.escape(piece) for piece in norm.split()))
    return re.compile(r"\s+".join(parts)) if masked and parts else None


def _masked_title_matches(expected_title: str, remote: str, is_filename: bool) -> bool:
    pattern = _masked_title_pattern(expected_title)
    if pattern is None:
        return False
    return any(pattern.fullmatch(normalize_text(variant)) for variant in title_variants(remote, is_filename=is_filename))


def _lone_descriptor_title_matches(expected_title: str, filename: str) -> bool:
    """
    A song called 'HD', 'Live' or 'Edit' in 'kyou1110 - Hd.flac' or '05 Live.mp3'. Filename
    parsing reads such a last part as a version of the name before it ('kyou1110 (Hd)'), so
    only the whole last part of a name with at most one part before it (the artist) is compared.
    """
    expected = _canonical_title(unicodedata.normalize("NFC", str(expected_title or "")).strip())
    if not expected or not _is_descriptor(expected):
        return False
    stem = _strip_audio_extension(unicodedata.normalize("NFC", str(filename or "")).strip())
    parts = _split_outside_brackets(_strip_track_token(_DASH_RE.sub("-", stem))[0])
    if not parts or len(parts) > 2:
        return False
    return normalize_text(_strip_track_token(parts[-1])[0]) == normalize_text(expected)


@lru_cache(maxsize=65536)
def title_match_score(expected_title: str, remote: str, is_filename: bool = True) -> float:
    """
    Best base-title similarity between an expected (MusicBrainz or tag) title and a remote
    filename or title, over title_variants of both. 0.0 when no pair has compatible versions,
    or when a remote reading carries numbers the expected title lacks and no reading carries
    exactly its numbers ('3 Year Coma' for '4 Year Coma'). An unbracketed 'Gunshotta Lone Remix'
    matches 'Gunshotta (Lone remix)'. A masked title ('my fuXX Sound') scores 0.95 against
    the spelled-out one.
    """
    score = _title_similarity(expected_title, remote, is_filename)
    if score < _MASKED_TITLE_SCORE and _masked_title_matches(expected_title, remote, is_filename):
        return _MASKED_TITLE_SCORE
    return score


def _title_similarity(expected_title: str, remote: str, is_filename: bool) -> float:
    if is_filename and _lone_descriptor_title_matches(expected_title, remote):
        return 1.0
    expected = [parse_track_title_structure(v, is_filename=False)
                for v in title_variants(expected_title, is_filename=False)]
    remote_parsed = [parse_track_title_structure(v, is_filename=False)
                     for v in title_variants(remote, is_filename=is_filename)]
    best = 0.0
    for p_exp in expected:
        exp_base = p_exp["base_norm"]
        if not exp_base:
            continue
        exp_numbers = _title_numbers(exp_base)
        remote_numbers = [_title_numbers(p["base_norm"]) for p in remote_parsed]
        # '3 Year Coma.flac' is not '4 Year Coma' although 'Year Coma' is similar; '23 22 8.mp3'
        # is track 23 '22/8' because one reading carries exactly the expected numbers.
        if exp_numbers and any(nums - exp_numbers for nums in remote_numbers) and exp_numbers not in remote_numbers:
            continue
        exp_full = normalize_text(f"{p_exp['base_title']} {p_exp['version_text'] or ''}")
        exp_version = (p_exp["version_text"] or "").replace(" ", "")
        for p_rem in remote_parsed:
            if not p_rem["base_norm"]:
                continue
            rem_full = normalize_text(f"{p_rem['base_title']} {p_rem['version_text'] or ''}")
            if p_exp["version_type"] == p_rem["version_type"] and exp_full.replace(" ", "") == rem_full.replace(" ", ""):
                return 1.0
            if are_versions_compatible(
                p_exp["version_type"], p_exp["version_text"], p_rem["version_type"], p_rem["version_text"],
            ):
                best = max(best, calculate_similarity(exp_base, p_rem["base_norm"]))
            elif p_rem["version_type"] is None and exp_version and rem_full.replace(" ", "").endswith(exp_version):
                # An unbracketed version tail: 'Affinity Cloud Four Four mix' for 'Affinity (Cloud Four Four mix)'.
                if exp_full.replace(" ", "") == rem_full.replace(" ", ""):
                    return 1.0
                full_similarity = calculate_similarity(exp_full, rem_full)
                if full_similarity >= 0.90:
                    best = max(best, full_similarity)
            if best == 1.0:
                return best
    return best


# Separators between credited artists. Word separators need a name on both sides, so
# 'And One', 'X Ambassadors' and 'Malcolm X' stay whole.
_CREDIT_PUNCT_SEPARATOR_RE = re.compile(r"\s*(?:[,;&+]|\s/\s)\s*")
_CREDIT_WORD_SEPARATOR_RE = re.compile(
    r"(?<=\S)\s+(?:feat\b\.?|ft\b\.?|featuring\b|with\b|and\b|x\b|vs\b\.?|versus\b)\s*(?=\S)",
    re.IGNORECASE,
)
_LEADING_FEATURE_RE = re.compile(r"^(?:feat\b\.?|ft\b\.?|featuring\b)\s*", re.IGNORECASE)


def _credit_display_names(credit: str) -> List[str]:
    text = unicodedata.normalize("NFC", str(credit or "")).strip()
    names = []
    for part in _CREDIT_PUNCT_SEPARATOR_RE.split(text):
        for name in _CREDIT_WORD_SEPARATOR_RE.split(part):
            name = _LEADING_FEATURE_RE.sub("", name.strip()).strip(" -,;:")
            if name:
                names.append(name)
    return names


def _normalize_artist_name(name: str) -> str:
    norm = normalize_text(name)
    return norm[4:] if norm.startswith("the ") and len(norm) > 4 else norm


@lru_cache(maxsize=65536)
def split_artist_credit(credit: str) -> Tuple[str, ...]:
    """
    Normalized individual artist names of a credit string, primary first:
    'Bianca Oblivion ft. Lunice & Lamsi' -> ('bianca oblivion', 'lunice', 'lamsi').
    Splits on feat./ft./featuring/with/&/and/,/;/x/vs./+/' / '; a leading 'the' is dropped.
    """
    names = []
    for name in _credit_display_names(credit):
        norm = _normalize_artist_name(name)
        if norm and norm not in names:
            names.append(norm)
    return tuple(names)


def primary_artist(credit: str) -> str:
    """Display name of the primary credited artist for search queries: 'Lone feat. Bikôkô' -> 'Lone'."""
    names = _credit_display_names(credit)
    return names[0] if names else str(credit or "").strip()


def _artist_keys(credit: str) -> Set[str]:
    """
    Normalized names of a credit, its full form, the names without a bracketed note
    ('Lone (UK electronic artist)' -> 'lone'), and cheap 'μ-Ziq'/'µ-Ziq'/'u-Ziq' spellings.
    """
    raw_names = _credit_display_names(credit) + [str(credit or "").strip()]
    keys: Set[str] = set()
    for name in raw_names:
        spellings = [name]
        unbracketed = re.sub(r"\s+", " ", _BRACKET_GROUP_RE.sub(" ", name)).strip()
        if unbracketed and unbracketed != name:
            spellings.append(unbracketed)
        # Greek mu (U+03BC) folds to 'm' and the micro sign (U+00B5) to 'u'; peers use both and 'u'.
        for spelling in list(spellings):
            if "\u03bc" in spelling or "\u00b5" in spelling:
                spellings += [spelling.replace("\u03bc", sub).replace("\u00b5", sub) for sub in ("u", "m", "mu")]
        for spelling in spellings:
            norm = _normalize_artist_name(spelling)
            if norm:
                keys.add(norm)
    return keys


def _artist_names_match(first: str, second: str) -> bool:
    if first == second or first.replace(" ", "") == second.replace(" ", ""):
        return True
    first_tokens, second_tokens = set(first.split()), set(second.split())
    smaller, larger = sorted((first_tokens, second_tokens), key=len)
    # Token containment needs a name of two or more words. One shared word names another
    # artist: 'Lone' vs 'Lone Wolf', or 'iron' (from 'Iron & Wine') vs 'Iron Maiden'.
    if len(smaller) >= 2 and smaller <= larger:
        return True
    return calculate_similarity(first, second) >= 0.90


def artist_credit_matches(expected_credit: str, remote_artist: str, aliases: Iterable[str] = ()) -> bool:
    """
    True when any credited name (or alias) of expected_credit matches any name in remote_artist:
    exact normalized names, token-subset containment of a name of two or more words, or
    similarity >= 0.9. A leading 'the' and a bracketed note are ignored.
    'Zeb Samuels & & Kiko Continentino' matches 'Zeb Samuels'; 'Lone' does not match 'Lone Wolf',
    and 'Iron & Wine' does not match 'Iron Maiden'.
    """
    remote_keys = _artist_keys(remote_artist)
    if not remote_keys:
        return False
    expected_keys: Set[str] = set()
    for credit in (expected_credit, *aliases):
        if credit:
            expected_keys |= _artist_keys(credit)
    return any(_artist_names_match(e, r) for e in expected_keys for r in remote_keys)


def extract_dir_and_filename(full_path: str) -> Tuple[str, str]:
    """Splits a Windows or POSIX path into (directory_path, filename)."""
    clean_p = full_path.replace("/", "\\")
    parts = clean_p.rsplit("\\", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


class FilenameUtils:
    """Utilities for sanitizing, decoding, and validating file paths."""

    @staticmethod
    def sanitize(name: str, max_bytes: int = 240, default_ext: str = "") -> str:
        """Sanitizes filename for cross-platform filesystem safety while preserving Unicode."""
        name = re.sub(r'[\\/*?:"<>|]', '_', name)
        name = name.strip('. \t\r\n')
        if not name:
            name = f"download_{int(time.time())}{default_ext}"
        encoded = name.encode("utf-8")
        if len(encoded) > max_bytes:
            name = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return name

    @staticmethod
    def clean_spaces(text: str) -> str:
        """Collapses consecutive spaces into a single space."""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def decode(raw_name: str) -> str:
        """Decodes raw or double percent-encoded strings and fixes Latin-1 mojibake."""
        decoded = urllib.parse.unquote(urllib.parse.unquote(raw_name))
        try:
            decoded = decoded.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return decoded
