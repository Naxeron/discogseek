"""
Unified Mutagen audio inspection, metadata extraction, and quality scoring.
"""

import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Set, Dict, Optional, Tuple, Any

import mutagen
import mutagen.mp3
import mutagen.aiff

from discogseek.core.constants import (
    LOSSLESS_EXTENSIONS,
    VA_DIR_MARKERS,
)
from discogseek.core.release_metadata import is_various_artists
from discogseek.core.text import (
    strip_track_number_and_artist,
    normalize_text
)


# Bump when analyze_file reads tags differently, so cached rows can be re-read.
# Version 2: fixed tag precedence (not mutagen's hash-ordered Vorbis keys), MP4
# 'trkn'/'disk' and MusicBrainz freeform atoms, APEv2 'Track', separate ID3
# multi-value frames, and track totals. Version 3: stream info (duration, bitrate,
# sample rate) of files without any tags. See UnifiedCacheManager for which older
# rows are re-read.
AUDIO_EXTRACTOR_VERSION = 3


@dataclass
class AudioMetadata:
    """Unified representation of audio file metadata, tags, and stream specs."""
    path: Path
    file_type: str = ""
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    track_number: str = ""
    disc_number: int = 1
    total_discs: int = 1
    year: str = ""
    genres: List[str] = field(default_factory=list)
    # Total tracks from 'n/N' track tags, TRACKTOTAL/TOTALTRACKS or MP4 trkn.
    track_total: Optional[int] = None
    # True when the file could not be read; its fallback metadata must not be cached.
    read_error: bool = False
    # The analyze_file version that produced these tags (older cached rows report 1).
    extractor_version: int = AUDIO_EXTRACTOR_VERSION

    # MusicBrainz Embedded Tag IDs
    mb_track_ids: Set[str] = field(default_factory=set)
    mb_rec_ids: Set[str] = field(default_factory=set)
    mb_artist_ids: Set[str] = field(default_factory=set)
    mb_release_ids: Set[str] = field(default_factory=set)

    # Audio Stream Parameters
    bitrate_kbps: int = 0
    bit_depth: int = 0
    sample_rate: int = 0
    channels: int = 2
    duration: float = 0.0
    is_lossless: bool = False
    format_label: str = ""
    quality_score: int = 0

    @property
    def norm_title(self) -> str:
        return normalize_text(self.title)

    @property
    def norm_artist(self) -> str:
        return normalize_text(self.artist)

    @property
    def norm_album(self) -> str:
        return normalize_text(self.album)

    @property
    def format(self) -> str:
        return self.format_label or self.file_type or ""

    @property
    def bitrate(self) -> int:
        return self.bitrate_kbps


WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10,
}
SIDE_MAP = {
    "a": 1, "b": 2, "c": 3, "d": 4,
    "e": 5, "f": 6, "g": 7, "h": 8,
}
DISC_DIR_PATTERN = re.compile(
    r"^(?:(?:vinyl|lp)\s+)?(?:disc|cd|disk|vinyl|lp|side)\s*[-_.]?\s*(?:(\d{1,2})|([a-hA-H])|(one|two|three|four|five|six|seven|eight|nine|ten))\b(?:\s*[-_.:(\[].*)?$",
    re.IGNORECASE
)

# Tag keys in precedence order. A file may carry several spellings with different
# values (Picard's ALBUMARTIST next to foobar's 'ALBUM ARTIST'); the first present
# key wins, independent of mutagen's key order (hash-randomized for Vorbis comments).
TITLE_KEYS = ("TIT2", "TITLE", "\xa9NAM", "TXXX:TITLE")
ALBUM_KEYS = ("TALB", "ALBUM", "\xa9ALB", "TXXX:ALBUM")
TRACK_KEYS = ("TRCK", "TRACKNUMBER", "TRKN", "TRACK", "TXXX:TRACKNUMBER")
TRACK_TOTAL_KEYS = ("TRACKTOTAL", "TOTALTRACKS", "TXXX:TRACKTOTAL", "TXXX:TOTALTRACKS")
DISC_KEYS = (
    "TPOS", "DISCNUMBER", "DISK", "DISC", "TXXX:DISCNUMBER", "TXXX:DISC",
    "TXXX:PART OF SET", "TXXX:PARTOFSET", "\xa9DISK", "WM/PARTOFSET",
)
DISC_TOTAL_KEYS = ("DISCTOTAL", "TOTALDISCS", "TXXX:DISCTOTAL", "TXXX:TOTALDISCS")
YEAR_KEYS = ("TDRC", "DATE", "\xa9DAY", "TYER", "YEAR")
ALBUM_ARTIST_KEYS = (
    "TPE2", "ALBUMARTIST", "AART", "TXXX:ALBUMARTIST", "ALBUM ARTIST",
    "TXXX:ALBUM ARTIST", "ALBUM_ARTIST",
)
ARTIST_KEYS = ("TPE1", "ARTIST", "\xa9ART", "TXXX:ARTIST", "PERFORMER", "TOPE")
COMPILATION_KEYS = ("TCMP", "CPIL", "COMPILATION", "TXXX:COMPILATION")
GENRE_KEYS = ("TCON", "GENRE", "\xa9GEN")
# MusicBrainz IDs from every spelling are combined. MP4 freeform atoms
# ('----:com.apple.iTunes:MusicBrainz Album Id') are keyed by their name.
MB_RELEASE_KEYS = ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ ALBUM ID", "TXXX:MUSICBRAINZ ALBUM ID")
MB_RECORDING_KEYS = (
    "MUSICBRAINZ_TRACKID", "MUSICBRAINZ TRACK ID", "TXXX:MUSICBRAINZ TRACK ID",
    "MUSICBRAINZ_RECORDINGID", "MUSICBRAINZ RECORDING ID", "TXXX:MUSICBRAINZ RECORDING ID",
)
MB_ARTIST_KEYS = ("MUSICBRAINZ_ARTISTID", "MUSICBRAINZ ARTIST ID", "TXXX:MUSICBRAINZ ARTIST ID")
MB_RELEASE_TRACK_KEYS = (
    "MUSICBRAINZ_RELEASETRACKID", "MUSICBRAINZ RELEASE TRACK ID", "TXXX:MUSICBRAINZ RELEASE TRACK ID",
)
_MP4_FREEFORM_PREFIX = re.compile(r"^----:[^:]*:")


def _tag_key(key: Any) -> str:
    """Upper-case tag key; an MP4 freeform atom is named by its suffix."""
    return _MP4_FREEFORM_PREFIX.sub("", str(key).upper().strip())


def _tag_texts(value: Any) -> List[str]:
    """
    Text values of one tag. ID3 frames list each value in .text (str() would join
    them with NUL); MP4 freeform atoms hold UTF-8 bytes; APEv2 separates values with NUL.
    """
    text = getattr(value, "text", None)
    if isinstance(text, (list, tuple)):
        items = list(text)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    texts = []
    for item in items:
        if isinstance(item, (tuple, list)):
            continue  # MP4 (number, total) pairs are read by _tag_pair
        if isinstance(item, (bytes, bytearray)):
            item = bytes(item).decode("utf-8", "replace")
        for part in str(item).split("\x00"):
            part = part.strip()
            if part:
                texts.append(part)
    return texts


def _tag_pair(value: Any) -> Optional[Tuple[int, int]]:
    """(number, total) from an MP4 'trkn'/'disk' value [(3, 10)]; total is 0 when unknown."""
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    if items and isinstance(items[0], (tuple, list)) and items[0]:
        try:
            number = int(items[0][0])
            total = int(items[0][1]) if len(items[0]) > 1 and items[0][1] else 0
            return number, total
        except (TypeError, ValueError):
            return None
    return None


def _split_number(raw: str) -> Tuple[str, Optional[int]]:
    """'3/12' -> ('3', 12); '3' -> ('3', None)."""
    if "/" not in raw:
        return raw.strip(), None
    number, total = raw.split("/", 1)
    digits = re.sub(r"[^\d]", "", total)
    return number.strip(), (int(digits) if digits and int(digits) > 0 else None)


def extract_artist_title_from_filename(stem: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts artist and title from filename stem when untagged and directory heuristics fail.
    Guards against extracting track numbers or single-element titles as artists.
    """
    normalized = re.sub(r"[\u2010-\u2015\u2212~]", " - ", stem)
    parts = [p.strip() for p in normalized.split(" - ") if p.strip()]
    if len(parts) < 2:
        return None, None

    p0_clean = re.sub(r"^(?:[a-zA-Z]?\d{1,4}|\d+[-._]\d+)$", "", parts[0]).strip()
    if not p0_clean:
        if len(parts) >= 3:
            cand_artist = parts[1].strip()
            cand_title = " - ".join(parts[2:]).strip()
            if cand_artist and not cand_artist.isdigit():
                return cand_artist, cand_title
            return None, cand_title
        return None, parts[1].strip()
    else:
        cand_artist = re.sub(r"^(?:[a-zA-Z]?\d{1,4}|\d+[-._]\d+)[\s._\-]+", "", parts[0]).strip()
        cand_title = " - ".join(parts[1:]).strip()
        if cand_artist and not re.match(r"^\d+$", cand_artist):
            return cand_artist, cand_title
        return None, None


# Formats mutagen.File identifies; None for one of these means the file was unreadable.
_MUTAGEN_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".alac", ".ogg", ".opus",
    ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv", ".dsf",
}


class AudioQualityAnalyzer:
    """Inspects audio stream parameters and computes standardized quality scores."""

    @staticmethod
    def _read_tags(mf: Any, meta: AudioMetadata) -> bool:
        """Fill tag fields of meta by fixed key precedence; returns the compilation flag."""
        tags = getattr(mf, "tags", None) if mf is not None else None
        if tags is None or not hasattr(tags, "items"):
            return False
        values: Dict[str, List[Any]] = {}
        try:
            for key, value in tags.items():
                values.setdefault(_tag_key(key), []).append(value)
        except Exception:
            return False

        def texts(key: str) -> List[str]:
            found: List[str] = []
            for value in values.get(key, []):
                found.extend(_tag_texts(value))
            return found

        def first(keys: Tuple[str, ...]) -> str:
            for key in keys:
                found = texts(key)
                if found:
                    return found[0]
            return ""

        meta.title = first(TITLE_KEYS)
        meta.album = first(ALBUM_KEYS)
        meta.year = first(YEAR_KEYS)
        meta.album_artist = first(ALBUM_ARTIST_KEYS)
        meta.artist = first(ARTIST_KEYS)

        for key in TRACK_KEYS:
            pair = _tag_pair(values[key][0]) if values.get(key) else None
            if pair is not None:
                if pair[0] > 0:
                    meta.track_number = str(pair[0])
                    meta.track_total = pair[1] or None
                    break
                continue
            found = texts(key)
            if found:
                number, total = _split_number(found[0])
                if number:
                    meta.track_number = number
                    meta.track_total = total
                    break
        if meta.track_total is None:
            digits = re.sub(r"[^\d]", "", first(TRACK_TOTAL_KEYS))
            if digits and int(digits) > 0:
                meta.track_total = int(digits)

        for key in DISC_KEYS:
            pair = _tag_pair(values[key][0]) if values.get(key) else None
            if pair is not None:
                if pair[0] > 0:
                    meta.disc_number = pair[0]
                if pair[1] > 0:
                    meta.total_discs = pair[1]
                break
            found = texts(key)
            if not found:
                continue
            number, total = _split_number(found[0])
            digits = re.sub(r"[^\d]", "", number)
            if digits and int(digits) > 0:
                meta.disc_number = int(digits)
            if total:
                meta.total_discs = total
            break
        digits = re.sub(r"[^\d]", "", first(DISC_TOTAL_KEYS))
        if digits and int(digits) > 0:
            meta.total_discs = int(digits)

        for key in GENRE_KEYS:
            for item in texts(key):
                for part in re.split(r"[;/]|\s{2,}", item):
                    part = part.strip()
                    if part and part not in meta.genres:
                        meta.genres.append(part)
            if meta.genres:
                break

        for keys, target in (
            (MB_RELEASE_KEYS, meta.mb_release_ids), (MB_RECORDING_KEYS, meta.mb_rec_ids),
            (MB_ARTIST_KEYS, meta.mb_artist_ids), (MB_RELEASE_TRACK_KEYS, meta.mb_track_ids),
        ):
            for key in keys:
                target.update(texts(key))
        for key, frames in values.items():
            if key.startswith("UFID:HTTP://MUSICBRAINZ.ORG"):
                for frame in frames:
                    try:
                        data = getattr(frame, "data", None)
                        ufid = bytes(data).decode("utf-8", errors="ignore") if data is not None else str(frame)
                    except Exception:
                        continue
                    if ufid.strip():
                        meta.mb_rec_ids.add(ufid.strip())

        return first(COMPILATION_KEYS).lower() in ("1", "true", "yes")

    @staticmethod
    def analyze_file(file_path: Path) -> AudioMetadata:
        """
        Inspects an audio file via Mutagen and extracts tags, bitrate, bit depth,
        sample rate, lossless status, and quality score.
        """
        path_str = str(file_path)
        ext = file_path.suffix.lower()
        meta = AudioMetadata(path=file_path, file_type=ext)

        try:
            mf = mutagen.File(path_str)
        except Exception:
            mf = None
            meta.read_error = True
        if mf is None and ext in _MUTAGEN_EXTENSIONS:
            # A transient I/O error or a file still being copied: the folder and
            # filename fallback below is only good for this scan.
            meta.read_error = True

        # A mutagen file object without tags is falsy (it is a mapping of its tags),
        # but its stream info is still valid: compare with None, never by truth value.
        type_name = type(mf).__name__ if mf is not None else "Unknown"
        mime = getattr(mf, "mime", [""])[0] if (mf is not None and hasattr(mf, "mime")) else ""

        is_compilation = AudioQualityAnalyzer._read_tags(mf, meta)

        if meta.total_discs < meta.disc_number:
            meta.total_discs = meta.disc_number

        if is_compilation and not meta.album_artist:
            meta.album_artist = "Various Artists"

        if meta.album_artist and is_various_artists(meta.album_artist):
            meta.album_artist = "Various Artists"

        # Fallback metadata from filename / path hierarchy
        filename_no_ext = file_path.stem

        parent_name = file_path.parent.name
        grandparent_name = file_path.parent.parent.name if file_path.parent != file_path.parent.parent else ""
        greatgrandparent_name = (
            file_path.parent.parent.parent.name
            if file_path.parent.parent != file_path.parent.parent.parent
            else ""
        )

        is_disc_subfolder = False
        disc_from_folder = None
        m_disc = DISC_DIR_PATTERN.match(parent_name.strip())
        if m_disc:
            is_disc_subfolder = True
            if m_disc.group(1):
                disc_from_folder = int(m_disc.group(1))
            elif m_disc.group(2):
                disc_from_folder = SIDE_MAP.get(m_disc.group(2).lower(), 1)
            elif m_disc.group(3):
                disc_from_folder = WORD_NUMS.get(m_disc.group(3).lower(), 1)

        if is_disc_subfolder and disc_from_folder:
            if meta.disc_number == 1:
                meta.disc_number = disc_from_folder
            if meta.total_discs < meta.disc_number:
                meta.total_discs = meta.disc_number

        if is_disc_subfolder:
            eff_album_folder = grandparent_name
            eff_artist_folder = greatgrandparent_name
        else:
            eff_album_folder = parent_name
            eff_artist_folder = grandparent_name

        if not meta.title:
            meta.title = strip_track_number_and_artist(filename_no_ext)
        if not meta.album or (meta.album and DISC_DIR_PATTERN.match(meta.album.strip())):
            if eff_album_folder and eff_album_folder.lower() not in ("music", "downloads", "library", "tracks", "singles"):
                meta.album = eff_album_folder

        norm_parent = eff_album_folder.lower().strip()
        norm_grandparent = eff_artist_folder.lower().strip()
        is_va_dir = (
            norm_parent in VA_DIR_MARKERS
            or norm_grandparent in VA_DIR_MARKERS
            or bool(re.match(r"^(?:va|v\.a\.|various\s*arti[st]{2,4}s?)\b", norm_parent))
            or bool(re.match(r"^(?:va|v\.a\.|various\s*arti[st]{2,4}s?)\b", norm_grandparent))
            or norm_parent.startswith("va - ")
            or norm_parent.startswith("va-")
            or norm_parent.startswith("va ")
            or norm_parent.startswith("v.a. - ")
            or norm_parent.startswith("various artists - ")
            or norm_parent.startswith("various - ")
            or "[va]" in norm_parent
            or "(va)" in norm_parent
            or norm_parent.endswith(" [va]")
            or norm_parent.endswith(" (va)")
        )
        if is_va_dir and not meta.album_artist:
            meta.album_artist = "Various Artists"

        if not meta.artist:
            if " - " in eff_album_folder:
                parts = eff_album_folder.split(" - ", 1)
                first_part = parts[0].strip()
                if (
                    first_part.lower() in ("va", "v.a.", "various", "various artists")
                    or first_part.lower() in VA_DIR_MARKERS
                    or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", first_part.lower()))
                ):
                    if not meta.album_artist:
                        meta.album_artist = "Various Artists"
                    if not meta.album or meta.album == eff_album_folder:
                        meta.album = parts[1].strip()
                else:
                    meta.artist = first_part
                    if not meta.album or meta.album == eff_album_folder:
                        meta.album = parts[1].strip()
            elif eff_artist_folder and eff_artist_folder.lower() not in ("music", "downloads", "library"):
                if (
                    eff_artist_folder.lower() in VA_DIR_MARKERS
                    or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", eff_artist_folder.lower()))
                ):
                    if not meta.album_artist:
                        meta.album_artist = "Various Artists"
                else:
                    meta.artist = eff_artist_folder

        if not meta.artist:
            cand_artist, cand_title = extract_artist_title_from_filename(filename_no_ext)
            if cand_artist:
                meta.artist = cand_artist
                if not meta.title or meta.title == filename_no_ext:
                    if cand_title:
                        meta.title = strip_track_number_and_artist(cand_title)

        if meta.artist and (
            meta.artist.strip().lower() in ("va", "v.a.", "various", "various artists")
            or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", meta.artist.strip().lower()))
        ):
            meta.artist = "Various Artists"
            if not meta.album_artist:
                meta.album_artist = "Various Artists"

        # Extract Audio Stream Specs
        if mf is not None and hasattr(mf, "info") and mf.info is not None:
            info = mf.info
            meta.duration = getattr(info, "length", 0.0)
            meta.sample_rate = getattr(info, "sample_rate", 0)
            meta.channels = getattr(info, "channels", 2)
            meta.bit_depth = getattr(info, "bits_per_sample", 0)
            raw_bitrate = getattr(info, "bitrate", 0)
            if raw_bitrate:
                meta.bitrate_kbps = int(raw_bitrate / 1000)

        # Quality scoring & format categorization
        if ext in LOSSLESS_EXTENSIONS or type_name in ("FLAC", "WAVE", "AIFF", "MonkeysAudio", "WavPack") or "flac" in mime or "wav" in mime:
            meta.is_lossless = True
            meta.bit_depth = meta.bit_depth or 16
            meta.sample_rate = meta.sample_rate or 44100
            if meta.bit_depth > 16:
                meta.format_label = f"FLAC {meta.bit_depth}-bit/{meta.sample_rate}Hz"
                meta.quality_score = 115
            else:
                meta.format_label = "FLAC (Lossless 16-bit)"
                meta.quality_score = 100
        elif ext in (".mp4", ".m4a") or type_name == "MP4" or "mp4" in mime or "m4a" in mime:
            codec = getattr(mf.info, "codec", "") if (mf is not None and hasattr(mf, "info")) else ""
            if codec == "alac" or meta.bit_depth > 0:
                meta.is_lossless = True
                meta.format_label = f"ALAC {meta.bit_depth or 16}-bit"
                meta.quality_score = 100
            else:
                meta.is_lossless = False
                bitrate_label = f"{meta.bitrate_kbps}kbps" if meta.bitrate_kbps else "AAC"
                meta.format_label = f"AAC {bitrate_label}"
                meta.quality_score = min(75, int(meta.bitrate_kbps / 4)) if meta.bitrate_kbps else 40
        elif ext == ".mp3" or type_name == "MP3" or "mp3" in mime or "mpeg" in mime:
            meta.is_lossless = False
            if meta.bitrate_kbps >= 320:
                meta.format_label = "MP3 320kbps"
                meta.quality_score = 80
            elif meta.bitrate_kbps >= 240:
                meta.format_label = f"MP3 ~{meta.bitrate_kbps}kbps (V0)"
                meta.quality_score = 70
            elif meta.bitrate_kbps >= 192:
                meta.format_label = f"MP3 {meta.bitrate_kbps}kbps"
                meta.quality_score = 50
            elif meta.bitrate_kbps > 0:
                meta.format_label = f"MP3 {meta.bitrate_kbps}kbps"
                meta.quality_score = 30
            else:
                meta.format_label = "MP3"
                meta.quality_score = 35
        elif ext in (".ogg", ".opus") or type_name in ("OggVorbis", "OggOpus"):
            meta.is_lossless = False
            codec_name = "Opus" if (ext == ".opus" or type_name == "OggOpus") else "Vorbis"
            if meta.bitrate_kbps >= 256:
                meta.format_label = f"{codec_name} ~{meta.bitrate_kbps}kbps"
                meta.quality_score = 75
            elif meta.bitrate_kbps >= 160:
                meta.format_label = f"{codec_name} {meta.bitrate_kbps}kbps"
                meta.quality_score = 60
            elif meta.bitrate_kbps > 0:
                meta.format_label = f"{codec_name} {meta.bitrate_kbps}kbps"
                meta.quality_score = 40
            else:
                meta.format_label = codec_name
                meta.quality_score = 45
        else:
            meta.format_label = ext.upper().lstrip(".") or "Audio"
            meta.quality_score = 40

        return meta

    @staticmethod
    def calculate_quality_score(meta: AudioMetadata) -> int:
        """Calculates quality score from an AudioMetadata object."""
        if meta.is_lossless:
            if meta.bit_depth and meta.bit_depth > 16:
                return 115
            return 100
        if meta.bitrate_kbps >= 320:
            return 80
        elif meta.bitrate_kbps >= 240:
            return 70
        elif meta.bitrate_kbps >= 192:
            return 50
        elif meta.bitrate_kbps > 0:
            return 30
        return 35

    @staticmethod
    def determine_stream_quality(file_item: Dict[str, Any]) -> Tuple[str, int]:
        """
        Evaluates audio format, bit depth, sample rate, and bitrate from slskd file attributes.
        Returns (format_label, quality_score).
        """
        fn = file_item.get("filename", "") or file_item.get("base_filename", "")
        ext = Path(fn).suffix.lower()
        bit_rate = file_item.get("bitRate", 0)
        bit_depth = file_item.get("bitDepth", 0)
        sample_rate = file_item.get("sampleRate", 0)

        if ext in LOSSLESS_EXTENSIONS:
            if bit_depth and bit_depth > 16:
                return f"FLAC {bit_depth}-bit/{sample_rate or 44100}Hz", 115
            return "FLAC (Lossless)", 100

        if ext in (".mp3", ".m4a", ".ogg", ".opus", ".mp4", ".wma"):
            if bit_rate >= 320:
                return "MP3 320kbps", 80
            elif bit_rate >= 240:
                return f"MP3 ~{bit_rate}kbps (V0)", 70
            elif bit_rate >= 192:
                return f"MP3 {bit_rate}kbps", 50
            elif bit_rate > 0:
                return f"MP3 {bit_rate}kbps", 30
            return ext.upper().lstrip("."), 40

        return "Audio File", 40
