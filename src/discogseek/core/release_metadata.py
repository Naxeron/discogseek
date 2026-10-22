"""Pure release metadata parsing shared by scanning, auditing, and downloads."""

import re
import unicodedata
from pathlib import Path
from typing import Any, Optional, Tuple

from discogseek.core.constants import VA_DIR_MARKERS
from discogseek.core.text import normalize_text


def is_unknown_album(title: Optional[str]) -> bool:
    """
    Missing album metadata and placeholders are not release titles, including
    Picard's '[standalone recordings]' and '[non-album tracks]'.
    """
    return normalize_text(title) in (
        "", "unknown album", "unknown", "standalone recordings", "non album tracks",
    )


# Vinyl sides A-H cover four LPs; a filename starting 'M83 - ' or 'U2 - ' names an artist.
_VINYL_POSITION_RE = re.compile(r"^\s*([A-Ha-h])\s*(\d{1,2})?\s*$")


def vinyl_position(value: Any) -> Optional[Tuple[str, int]]:
    """
    (side letter, number) of a vinyl position token: 'A1' -> ('A', 1), 'b02' -> ('B', 2),
    and a bare side 'B' -> ('B', 1). None for anything else ('1', '1-01', 'M83', 'U2').
    """
    if value is None:
        return None
    match = _VINYL_POSITION_RE.match(str(value))
    if not match:
        return None
    number = int(match.group(2)) if match.group(2) else 1
    if number < 1:
        return None
    return match.group(1).upper(), number


_FOLDER_DISC_NUMBER = r"(\d{1,2}(?!\d)|one|two|three|four|five|six|seven|eight|nine|ten)\b"
_FOLDER_DISC_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
# 'CD 1 & 2' and 'Disc 1-3' name several discs, not one.
_FOLDER_DISC_RANGE = r"(?!\s*(?:&|\+|-|–|and\b|to\b)\s*\d{1,2}\b)"
# 'CD 2', 'Disc 1 - Bonus Tracks', 'CD2 Greatest Hits', 'LP 2'
_DISC_FOLDER_NAME_RE = re.compile(
    r"^(?:disc|disk|cd|lp|vinyl)\s*[-_.]?\s*" + _FOLDER_DISC_NUMBER + _FOLDER_DISC_RANGE
    + r"(?:\s*(?:of|/)\s*\d{1,2})?(?:[\s\-_.:(\[].*)?$",
    re.IGNORECASE,
)
# 'Album (Disc 2)', 'Dreams [CD 4 of 5]', 'Buddha-Bar VI (CD2 - Rejoice)'; a bracket that starts
# with a label ('[ZEN CD 215]', '[WARP CD 6]') is a catalog number.
_DISC_FOLDER_BRACKET_RE = re.compile(
    r"[\(\[]\s*(?:disc|disk|cd)\s*[-_.]?\s*" + _FOLDER_DISC_NUMBER + _FOLDER_DISC_RANGE
    + r"\s*(?:(?:of|/)\s*\d{1,2}\s*)?(?:[\s\-:,][^\(\)\[\]]*)?[\)\]]",
    re.IGNORECASE,
)
# 'Album - CD 2', 'Album_CD2 [FLAC]', 'Anthology - disc 1 of 2'
_DISC_FOLDER_SUFFIX_RE = re.compile(
    r"(?:^|[\s_\-.,])(?:disc|disk|cd)\s*[-_.]?\s*" + _FOLDER_DISC_NUMBER
    + r"(?:\s*(?:of|/)\s*\d{1,2})?\s*(?:[\(\[][^\(\)\[\]]*[\)\]]\s*)*$",
    re.IGNORECASE,
)
# 'The Joshua Tree CD3 Remixes', 'Collection - CD 4 - Unreleased', 'Album-CD1-FLAC-2010-GRP'.
# Only searched outside brackets, where catalog numbers ('[WARP CD 6]', '(ZEN CD123)') live.
_DISC_FOLDER_INFIX_RE = re.compile(
    r"(?:^|(?<=[\s_\-.,]))(?:disc|disk|cd)\s*[-_.]?\s*"
    r"(\d{1,2}(?!\d)|(?:one|two|three|four|five|six|seven|eight|nine|ten)(?![a-z]))"
    + _FOLDER_DISC_RANGE + r"(?=$|[\s_\-.,:;])",
    re.IGNORECASE,
)
_FOLDER_BRACKET_GROUP_RE = re.compile(r"[\(\[\{][^\(\)\[\]\{\}]*[\)\]\}]")


def _without_bracket_groups(text: str) -> str:
    previous = None
    while previous != text:
        previous, text = text, _FOLDER_BRACKET_GROUP_RE.sub(" ", text)
    return text


def disc_number_from_folder(name: Optional[str]) -> Optional[int]:
    """
    Disc number (1..20) named by a release folder or disc subfolder: 'CD 2', 'Disc 1 - Bonus',
    'Album (Disc 2)', 'Album - CD2 [FLAC]', 'Collection CD1 Frank'. Years and catalog numbers
    are not discs: '[ZEN CD 215]', 'CD-2002', '(CD 1998)' and '[WARP CD 6]' give None, and so
    does a folder holding several discs ('cd 1 & 2'). Callers that know the release's medium
    count should also ignore a number above it.
    """
    if not name:
        return None
    text = str(name).strip()
    for pattern in (_DISC_FOLDER_NAME_RE, _DISC_FOLDER_BRACKET_RE, _DISC_FOLDER_SUFFIX_RE, _DISC_FOLDER_INFIX_RE):
        if pattern is _DISC_FOLDER_NAME_RE:
            match = pattern.match(text)
        elif pattern is _DISC_FOLDER_INFIX_RE:
            match = pattern.search(_without_bracket_groups(text))
        else:
            match = pattern.search(text)
        if not match:
            continue
        token = match.group(1).lower()
        value = _FOLDER_DISC_WORDS.get(token) or (int(token) if token.isdigit() else None)
        if value is not None and 1 <= value <= 20:
            return value
    return None


def parse_disc_and_track_number(
    raw_track: Any, filename: Optional[str] = None, meta_disc: Optional[int] = None,
    disc_count: Optional[int] = None,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Robustly parses (disc_number, track_number, total_tracks).
    Handles:
    - Standard: '1', '01', '1/12'
    - Disc-track prefix: '1-01', '1.01', '2-03', '01-02'
    - Vinyl side notation: 'A1', 'B1', 'A', 'B', 'Side A', 'Side B', 'Vinyl Side B'
    - Filename prefixes: '1-01 - Title.flac', '2.01 Song.flac', 'A1 Title.mp3', 'Side B 01.flac', '01.flac',
      '0101 - Title.flac' (disc 1, track 1)
    - Disc notation in raw_track or filename overrides default disc_num == 1.
    Filename vinyl prefixes need a side letter A-H and a separator, so 'M83 - Title.flac'
    and 'U2 - One.mp3' carry no position. A filename prefix whose number contradicts a
    numeric track tag is ignored. With disc_count, a 3-digit filename prefix 'DNN'
    ('205 - Title.flac') is disc D track NN when 1 <= D <= disc_count.
    """
    disc_num = meta_disc if (meta_disc is not None and meta_disc > 0) else None
    track_num = None
    total_tracks = None

    if raw_track is not None:
        raw_str = str(raw_track).strip()
        if "/" in raw_str:
            parts = raw_str.split("/", 1)
            raw_str = parts[0].strip()
            tot_str = re.sub(r"[^\d]", "", parts[1])
            if tot_str:
                try:
                    total_tracks = int(tot_str)
                except ValueError:
                    pass

        # Side notation with letter in tag: 'Side A', 'Side B', 'Side B 02', 'Vinyl Side B', 'Side B-01'
        m_side = re.match(
            r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*([a-zA-Z])(?:\s*[-_.]?\s*(\d{1,3}))?$",
            raw_str,
            re.IGNORECASE,
        )
        if m_side:
            letter = m_side.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord("A") + 1
            num_part = m_side.group(2)
            track_num = int(num_part) if num_part else 1
            return disc_num, track_num, total_tracks

        # Side notation with number in tag: 'Side 1', 'Side 2', 'Side 2 - 01'
        m_side_num = re.match(
            r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*(\d{1,2})(?:\s*[-_.]?\s*(\d{1,3}))?$",
            raw_str,
            re.IGNORECASE,
        )
        if m_side_num:
            d_val = int(m_side_num.group(1))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            num_part = m_side_num.group(2)
            track_num = int(num_part) if num_part else 1
            return disc_num, track_num, total_tracks

        # Disc/CD with number in tag: 'Disc 2 - 01', 'CD 2 - 01', 'Disc 2'
        m_disc = re.match(
            r"^(?:disc|cd|disk)\s*[-_.]?\s*(\d{1,2})(?:\s*[-_.:]\s*(\d{1,3})|\s+track\s+(\d{1,3}))?$",
            raw_str,
            re.IGNORECASE,
        )
        if m_disc:
            d_val = int(m_disc.group(1))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            num_part = m_disc.group(2) or m_disc.group(3)
            track_num = int(num_part) if num_part else 1
            return disc_num, track_num, total_tracks

        # Vinyl side: A1, B2, A, B
        m_vinyl = re.match(r"^([A-Za-z])(\d{1,3})?$", raw_str)
        if m_vinyl:
            letter = m_vinyl.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord("A") + 1
            num_part = m_vinyl.group(2)
            track_num = int(num_part) if num_part else 1
            return disc_num, track_num, total_tracks

        # Disc-track pattern: "1-01", "1.01", "2-03", "01-02"
        m_dt = re.match(r"^(\d{1,2})[-_.](\d{1,3})$", raw_str)
        if m_dt:
            d_val = int(m_dt.group(1))
            t_val = int(m_dt.group(2))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            track_num = t_val
            return disc_num, track_num, total_tracks

        # Standard digits
        digits_only = re.sub(r"[^\d]", "", raw_str)
        if digits_only:
            try:
                track_num = int(digits_only)
            except ValueError:
                pass

    # A track number from the tag outranks a filename prefix that names another position.
    tag_track = track_num

    def agrees(number: int) -> bool:
        return tag_track is None or number == tag_track

    # Inspect filename for disc/track information
    if filename:
        fn = Path(filename).name

        # Filename side notation with letter: 'Side B 01 - Song.flac', 'Side B - 02.mp3', 'Side B.flac'
        m_fn_side = re.match(
            r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*([a-zA-Z])(?:\s*[-_.]?\s*(\d{1,3}))?(?:[\s._\-]|$)",
            fn,
            re.IGNORECASE,
        )
        if m_fn_side and agrees(int(m_fn_side.group(2)) if m_fn_side.group(2) else 1):
            letter = m_fn_side.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord("A") + 1
            if track_num is None:
                track_num = int(m_fn_side.group(2)) if m_fn_side.group(2) else 1
            return disc_num, track_num, total_tracks

        # Filename side notation with number: 'Side 2 - 01 Song.flac'
        m_fn_side_num = re.match(
            r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*(\d{1,2})(?:\s*[-_.]?\s*(\d{1,3}))?(?:[\s._\-]|$)",
            fn,
            re.IGNORECASE,
        )
        if m_fn_side_num and agrees(int(m_fn_side_num.group(2)) if m_fn_side_num.group(2) else 1):
            d_val = int(m_fn_side_num.group(1))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            if track_num is None:
                track_num = int(m_fn_side_num.group(2)) if m_fn_side_num.group(2) else 1
            return disc_num, track_num, total_tracks

        # Filename Disc/CD prefix: 'Disc 2 - 01 Song.flac', 'CD 2 - 01 Song.flac'
        m_fn_disc = re.match(
            r"^(?:disc|cd|disk)\s*[-_.]?\s*(\d{1,2})\s*[-_.:\s]\s*(?:track\s+)?(\d{1,3})(?:[\s._\-]|$)",
            fn,
            re.IGNORECASE,
        )
        if m_fn_disc and agrees(int(m_fn_disc.group(2))):
            d_val = int(m_fn_disc.group(1))
            t_val = int(m_fn_disc.group(2))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            if track_num is None:
                track_num = t_val
            return disc_num, track_num, total_tracks

        # Filename vinyl notation: 'B1 Song.flac', 'B01 - Song.flac', 'A1 - Title.mp3'
        m_fn_vinyl = re.match(r"^([A-Ha-h])(\d{1,2})(?:[\s._\-]|$)", fn)
        if m_fn_vinyl and 1 <= int(m_fn_vinyl.group(2)) <= 30 and agrees(int(m_fn_vinyl.group(2))):
            letter = m_fn_vinyl.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord("A") + 1
            if track_num is None:
                track_num = int(m_fn_vinyl.group(2))
            return disc_num, track_num, total_tracks

        # Filename disc-track prefix: '2-01 Song.flac', '2.01 Song.flac', '02-01 Song.flac'
        m_fn_dt = re.match(r"^(\d{1,2})[-_.](\d{1,3})(?:[\s._\-]|$)", fn)
        if m_fn_dt and agrees(int(m_fn_dt.group(2))):
            d_val = int(m_fn_dt.group(1))
            t_val = int(m_fn_dt.group(2))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            if track_num is None:
                track_num = t_val
            return disc_num, track_num, total_tracks

        # Standard track number fallback from filename (only if track_num not already resolved)
        if track_num is None:
            m_fn = re.match(r"^(\d{1,4})(?:[\s._\-]|$)", fn)
            digits = m_fn.group(1) if m_fn else ""
            if len(digits) == 4:
                # '0101 - Title' is disc 1 track 1 and '0018 - Title' track 18; '1999 - Title' is a year.
                if digits.startswith("0") and 1 <= int(digits[:2]) <= 20 and int(digits[2:]) >= 1:
                    if disc_num is None or disc_num == 1:
                        disc_num = int(digits[:2])
                    track_num = int(digits[2:])
                elif digits.startswith("00") and int(digits) >= 1:
                    track_num = int(digits)
            elif (
                len(digits) == 3 and disc_count and not digits.startswith("0")
                and int(digits[0]) <= disc_count and int(digits[1:]) >= 1
            ):
                # Scene and web rips number multi-disc tracks 'DNN': '305' is disc 3 track 5.
                if disc_num is None or disc_num == 1:
                    disc_num = int(digits[0])
                track_num = int(digits[1:])
            elif digits:
                track_num = int(digits)

    if disc_num is None:
        disc_num = 1

    return disc_num, track_num, total_tracks


def format_artist_credit(ac_list: Any, default: str = "") -> str:
    """Formats a MusicBrainz artist-credit list into a clean display string."""
    if not ac_list:
        return default
    if isinstance(ac_list, str):
        return ac_list
    parts = []
    for c in ac_list:
        if isinstance(c, dict):
            name = c.get("name") or c.get("artist", {}).get("name", "")
            join = c.get("joinphrase", "")
            parts.append(name + join)
        else:
            parts.append(str(c))
    return "".join(parts).strip() or default


def is_various_artists(name: Optional[str]) -> bool:
    """Checks if an artist or tag string represents Various Artists."""
    if not name:
        return False
    # NFKC folds fullwidth 'ＶＡＲＩＯＵＳ' and 'Ｖ.Ａ.' to ASCII.
    norm = unicodedata.normalize("NFKC", name).strip().lower()
    return (
        norm in VA_DIR_MARKERS
        or norm in (
            "various artists", "various", "va", "v.a.", "v.a", "v. a.", "v a", "v/a",
            "compilation", "compilations",
        )
        or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", norm))
    )


def is_various_artists_directory(path: Path) -> bool:
    """Checks if a folder name or parent directory path matches compilation / VA patterns."""
    p_name = path.name.lower().strip()
    gp_name = path.parent.name.lower().strip() if path.parent != path else ""
    for name in (p_name, gp_name):
        if name in VA_DIR_MARKERS:
            return True
        if bool(re.match(r"^(?:va|v\.a\.|various\s*arti[st]{2,4}s?)\b", name)):
            return True
        if name.startswith(
            ("va - ", "va-", "va ", "v.a. - ", "various artists - ", "various - ", "[va]", "(va)")
        ):
            return True
        if "[va]" in name or "(va)" in name or name.endswith(" [va]") or name.endswith(" (va)"):
            return True
    return False


MISSING_TRACK_PATTERN = re.compile(
    r"^(?:Disc\s+(\d+)\s+)?Track\s+(\d+)\s*\(Missing\)$", re.IGNORECASE
)


def is_missing_track_placeholder(title: str) -> bool:
    """Recognize the gap placeholders emitted by a local library scan."""
    return bool(MISSING_TRACK_PATTERN.match(title))
