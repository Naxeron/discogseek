"""Pure release metadata parsing shared by scanning, auditing, and downloads."""

import re
from pathlib import Path
from typing import Any, Optional, Tuple

from discogseek.core.constants import VA_DIR_MARKERS


def parse_disc_and_track_number(
    raw_track: Any, filename: Optional[str] = None, meta_disc: Optional[int] = None
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Robustly parses (disc_number, track_number, total_tracks).
    Handles:
    - Standard: '1', '01', '1/12'
    - Disc-track prefix: '1-01', '1.01', '2-03', '01-02'
    - Vinyl side notation: 'A1', 'B1', 'A', 'B', 'Side A', 'Side B', 'Vinyl Side B'
    - Filename prefixes: '1-01 - Title.flac', '2.01 Song.flac', 'A1 Title.mp3', 'Side B 01.flac', '01.flac'
    - Disc notation in raw_track or filename overrides default disc_num == 1.
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

    # Inspect filename for disc/track information
    if filename:
        fn = Path(filename).name

        # Filename side notation with letter: 'Side B 01 - Song.flac', 'Side B - 02.mp3', 'Side B.flac'
        m_fn_side = re.match(
            r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*([a-zA-Z])(?:\s*[-_.]?\s*(\d{1,3}))?(?:[\s._\-]|$)",
            fn,
            re.IGNORECASE,
        )
        if m_fn_side:
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
        if m_fn_side_num:
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
        if m_fn_disc:
            d_val = int(m_fn_disc.group(1))
            t_val = int(m_fn_disc.group(2))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            if track_num is None:
                track_num = t_val
            return disc_num, track_num, total_tracks

        # Filename vinyl notation: 'B1 Song.flac', 'B01 - Song.flac', 'A1 - Title.mp3'
        m_fn_vinyl = re.match(r"^([A-Za-z])(\d{1,3})(?:[\s._\-]|$)", fn)
        if m_fn_vinyl:
            letter = m_fn_vinyl.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord("A") + 1
            if track_num is None:
                track_num = int(m_fn_vinyl.group(2))
            return disc_num, track_num, total_tracks

        # Filename disc-track prefix: '2-01 Song.flac', '2.01 Song.flac', '02-01 Song.flac'
        m_fn_dt = re.match(r"^(\d{1,2})[-_.](\d{1,3})(?:[\s._\-]|$)", fn)
        if m_fn_dt:
            d_val = int(m_fn_dt.group(1))
            t_val = int(m_fn_dt.group(2))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            if track_num is None:
                track_num = t_val
            return disc_num, track_num, total_tracks

        # Standard track number fallback from filename (only if track_num not already resolved)
        if track_num is None:
            m_fn = re.match(r"^(\d{1,3})(?:[\s._\-]|$)", fn)
            if m_fn:
                try:
                    track_num = int(m_fn.group(1))
                except ValueError:
                    pass

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
    norm = name.strip().lower()
    return (
        norm in VA_DIR_MARKERS
        or norm
        in ("various artists", "various", "va", "v.a.", "v/a", "compilation", "compilations")
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
