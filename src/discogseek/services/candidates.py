"""Index, match, and rank discovered Soulseek files without network or workflow state."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, Hashable, Iterable, List, Optional, Sequence, Set, Tuple

from discogseek.core.audio import AudioQualityAnalyzer, DISC_DIR_PATTERN
from discogseek.core.constants import AUDIO_EXTENSIONS, DIR_STOP_WORDS, SUPPORTING_EXTENSIONS
from discogseek.core.peer_policy import peer_key
from discogseek.core.release_metadata import (
    disc_number_from_folder,
    is_various_artists,
    parse_disc_and_track_number,
)
from discogseek.core.transfers import source_key
from discogseek.core.text import (
    _tokenize_words_cached,
    are_versions_compatible,
    artist_credit_matches,
    calculate_similarity,
    clean_tokens,
    extract_dir_and_filename,
    normalize_text,
    parse_track_title_structure,
    strip_track_number_and_artist,
    title_variants,
)


# A leading track token before the artist or title: '01 - ', '(02) ', '[03] ', 'A1. ', '1-05 '.
_LEADING_TRACK_TOKEN_RE = re.compile(
    r"^(?:\(\d{1,3}\)|\[\d{1,3}\]|[A-Ha-h]?\d{1,4}(?:[-_.]\d{1,3})?)(?:[\s._]*-+[\s._]*|[\s._]+)"
)
_ARTIST_SEPARATOR_RE = re.compile(r"\s+-+\s+")
_LEADING_YEAR_RE = re.compile(r"^[\(\[]?(?:19|20)\d{2}[\)\]]?(?:\s*[-._]\s*|\s+)")
_DISC_WORD = r"(?:\d{1,2}(?!\d)|one|two|three|four|five|six|seven|eight|nine|ten)\b"
_DISC_TOKEN_BRACKET_RE = re.compile(
    r"\s*[\(\[]\s*(?:disc|disk|cd)\s*[-_.]?\s*" + _DISC_WORD + r"[^\(\)\[\]]*[\)\]]", re.IGNORECASE,
)
_DISC_TOKEN_RE = re.compile(
    r"(?:^|[\s_\-.,]+)(?:disc|disk|cd)\s*[-_.]?\s*" + _DISC_WORD + r"(?:\s*(?:of|/)\s*\d{1,2})?",
    re.IGNORECASE,
)


def _has_run(words: Sequence[str], run: Sequence[str]) -> bool:
    """Whether run occurs as a contiguous word sequence inside words."""
    size = len(run)
    if not size or size > len(words):
        return False
    words, run = tuple(words), tuple(run)
    return any(words[index:index + size] == run for index in range(len(words) - size + 1))


@lru_cache(maxsize=4096)
def _alias_word_lists(aliases: FrozenSet[str]) -> Tuple[Tuple[str, ...], ...]:
    words = {tuple(normalize_text(alias).split()) for alias in aliases if alias}
    return tuple(sorted(run for run in words if run))


def alias_word_lists(aliases: Iterable[str]) -> Tuple[Tuple[str, ...], ...]:
    """Normalized word sequences of the artist's names, for word-boundary matching."""
    return _alias_word_lists(frozenset(alias for alias in (aliases or ()) if alias))


@lru_cache(maxsize=65536)
def filename_artist_prefix(base_filename: str) -> str:
    """The credited artist written before ' - ' in a filename, without its track token.

    '01 - Some Band - Afterglow.flac' and 'Some Band - 01 - Afterglow.mp3' give
    'Some Band'; '07 Afterglow.flac' gives ''.
    """
    stem = base_filename
    if Path(stem).suffix.lower() in AUDIO_EXTENSIONS:
        stem = Path(stem).stem
    stem = stem.replace("_-_", " - ").strip()
    stem = _LEADING_TRACK_TOKEN_RE.sub("", stem, count=1)
    parts = _ARTIST_SEPARATOR_RE.split(stem)
    if len(parts) < 2:
        return ""
    prefix = parts[0].strip()
    return "" if re.fullmatch(r"[\W\d_]*", prefix) else prefix


def directory_named_artist(dir_name: str) -> str:
    """The artist written before ' - ' in a release folder name ('Band - Album [FLAC]')."""
    parent, base = extract_dir_and_filename(dir_name or "")
    if DISC_DIR_PATTERN.match(base.strip()) and parent:
        base = extract_dir_and_filename(parent)[1]
    base = _LEADING_YEAR_RE.sub("", base.strip())
    parts = _ARTIST_SEPARATOR_RE.split(base)
    if len(parts) < 2:
        return ""
    prefix = parts[0].strip()
    if not prefix or prefix[0] in "([{" or re.fullmatch(r"[\W\d_]*", prefix):
        return ""
    return prefix


@lru_cache(maxsize=65536)
def _names_artist(name: str, aliases: FrozenSet[str]) -> bool:
    return bool(name) and artist_credit_matches("", name, aliases)


_LEADING_TAGS_RE = re.compile(r"^\s*(?:[\(\[\{][^\)\]\}]*[\)\]\}]\s*[-._]?\s*)+")
# A leading catalog number: 'ZIQ317 Machinedrum-...', 'WARPCD117 - ...'.
_LEADING_CATALOG_RE = re.compile(r"^[A-Za-z]{2,8}-?(?!(?:19|20)\d{2}\b)\d{2,6}[A-Za-z]?(?:\s*-+\s*|\s+)")
_EDGE_YEAR_RE = re.compile(r"^\s*(?:19|20)\d{2}\s+|\s+(?:19|20)\d{2}\s*$")
_BRACKETED_RE = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")
_FOLDER_SEGMENT_RE = re.compile(r"\s+-+\s+|_-_")


def _path_folders(path: str) -> List[str]:
    return [part for part in (path or "").replace("/", "\\").split("\\") if part.strip()]


def _folder_name_heads(part: str) -> List[str]:
    """Leading names of a folder name segment, cut at a scene '-', a bracket, an underscore or '. '."""
    heads = []
    scene = [token for token in re.split(r"-+", part.replace("_", " ")) if token.strip()]
    if len(scene) > 1:
        heads.append(scene[0])
    bracketed = re.split(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*", part, maxsplit=1)
    if len(bracketed) > 1 and bracketed[0].strip() and bracketed[1].strip():
        heads.append(bracketed[0])
    # 'Lone_Album Title (2024)': an underscore joins the artist to a spaced title.
    head, separator, rest = part.partition("_")
    if separator and " " in rest and " " not in head.strip():
        heads.append(head)
    head, separator, _ = part.partition(". ")
    if separator:
        heads.append(head)
    return heads


@lru_cache(maxsize=65536)
def _folder_names_artist(folder: str, aliases: FrozenSet[str]) -> bool:
    """Whether one folder name credits the artist.

    'Lone', 'Lone (UK)', 'Lone - Album (2024)', '[NONE] Lone - Album',
    'Album - Lone', 'Lone_Album Title', 'Machinedrum [2012] Album' and scene
    names ('Lone-Album-WEB-2024-GRP', 'Bikoko_x_Lone-Single-WEB') do. A name
    that only contains the artist's words is someone else: 'K-Lone',
    'The Lone Ranger', 'Lone Wolf - Cobra', 'Lone Sentinel'.
    """
    alias_lists = _alias_word_lists(aliases)
    if not any(_has_run(_tokenize_words_cached(folder), run) for run in alias_lists):
        return False
    text = _LEADING_TAGS_RE.sub("", folder)
    text = _LEADING_YEAR_RE.sub("", text.strip())
    text = _LEADING_CATALOG_RE.sub("", text)

    def names(part: str) -> bool:
        core = _EDGE_YEAR_RE.sub(" ", _BRACKETED_RE.sub(" ", part)).strip(" -_.")
        return bool(core) and _names_artist(core, aliases)

    if names(text):
        return True
    segments = [segment for segment in _FOLDER_SEGMENT_RE.split(text) if segment.strip()]
    return any(
        names(segment) or any(names(head) for head in _folder_name_heads(segment.strip()))
        for segment in segments
    )


def path_names_artist(path: str, artist_aliases: Iterable[str]) -> bool:
    """Whether a folder of the remote path credits the artist (see _folder_names_artist)."""
    aliases = frozenset(alias for alias in (artist_aliases or ()) if alias)
    return bool(aliases) and any(_folder_names_artist(folder, aliases) for folder in _path_folders(path))


def path_mentions_artist(path: str, artist_aliases: Iterable[str]) -> bool:
    """Whether the artist's name appears as whole words anywhere in the path (weaker evidence)."""
    words = _tokenize_words_cached(path)
    return any(_has_run(words, run) for run in alias_word_lists(artist_aliases))


def _title_numbers(text: str) -> Set[str]:
    return set(re.findall(r"\b\d+\b", text or ""))


def disc_folder_group(dir_name: str) -> Optional[str]:
    """Name of the release folder that a per-disc folder belongs to.

    'Album\\CD2' -> 'Album'; 'Artist\\Album (Disc 2)' -> 'Artist\\Album'. Any
    other folder gives None.
    """
    parent, base = extract_dir_and_filename(dir_name or "")
    base = base.strip()
    if not parent or not base:
        return None
    if DISC_DIR_PATTERN.match(base):
        return parent
    if disc_number_from_folder(base) is None:
        return None
    stem = _DISC_TOKEN_BRACKET_RE.sub("", base)
    stem = _DISC_TOKEN_RE.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -_.,")
    return f"{parent}\\{stem}" if stem else None


class CandidateFile:
    """Pre-parsed, indexed representation of a discovered remote Soulseek file."""
    __slots__ = (
        "user", "dir_name", "raw_file", "dir_info", "base_filename", "full_filename",
        "clean_base", "words", "clean_words", "sig_words", "concat",
        "p_struct", "is_audio", "is_supporting", "fmt_label", "fmt_score", "size",
        "_bases", "_position",
    )

    def __init__(self, user: str, dir_name: str, raw_file: Dict[str, Any], dir_info: Optional[Dict[str, Any]] = None):
        self.user = user
        self.dir_name = dir_name
        self.raw_file = raw_file
        self.dir_info = dir_info or {}
        fn = raw_file.get("full_filename") or raw_file.get("filename", "")
        self.full_filename = fn
        self.base_filename = raw_file.get("base_filename") or extract_dir_and_filename(fn)[1]
        self.size = raw_file.get("size", 0)
        self._bases: Optional[FrozenSet[str]] = None
        self._position: Optional[Tuple[Optional[int], Optional[int]]] = None
        ext = Path(self.base_filename).suffix.lower()
        self.is_audio = ext in AUDIO_EXTENSIONS
        self.is_supporting = ext in SUPPORTING_EXTENSIONS
        if self.is_audio:
            self.p_struct = parse_track_title_structure(self.base_filename)
            self.clean_base = self.p_struct["base_norm"] or strip_track_number_and_artist(self.base_filename)
            self.words = list(_tokenize_words_cached(self.p_struct["base_norm"] or self.base_filename))
            self.clean_words = list(_tokenize_words_cached(self.clean_base)) or self.words
            self.sig_words = {w for w in self.clean_words if len(w) >= 3 and w not in DIR_STOP_WORDS}
            self.concat = "".join(self.clean_words)
            self.fmt_label, self.fmt_score = AudioQualityAnalyzer.determine_stream_quality(raw_file)
        else:
            self.p_struct = None
            self.clean_base = ""
            self.words = []
            self.clean_words = []
            self.sig_words = set()
            self.concat = ""
            self.fmt_label, self.fmt_score = "", 0

    @property
    def folder(self) -> str:
        """The file's own remote folder (a disc folder for merged multi-disc releases)."""
        return extract_dir_and_filename(self.full_filename)[0] or self.dir_name

    def title_bases(self) -> FrozenSet[str]:
        """Base titles of every plausible reading of the filename ('2 Is 8', ' - ' suffixes)."""
        if self._bases is None:
            bases = {self.p_struct["base_norm"]} if self.p_struct and self.p_struct["base_norm"] else set()
            if self.is_audio:
                for variant in title_variants(self.base_filename):
                    base = parse_track_title_structure(variant, is_filename=False)["base_norm"]
                    if base:
                        bases.add(base)
            self._bases = frozenset(bases)
        return self._bases

    def position(self) -> Tuple[Optional[int], Optional[int]]:
        """(disc, track) written in the filename, tags or a disc folder; None where unknown."""
        if self._position is None:
            folder_disc = disc_number_from_folder(extract_dir_and_filename(self.folder)[1])
            raw_number = self.raw_file.get("track_number") or self.raw_file.get("track_num_int")
            try:
                disc, number, _ = parse_disc_and_track_number(
                    raw_number, filename=self.base_filename, meta_disc=folder_disc,
                )
            except Exception:
                disc, number = None, None
            # A file whose title is only a number carries no position ('1999.flac').
            stem = Path(self.base_filename).stem
            if raw_number is None and stem.isdecimal() and len(stem) == 4:
                number = None
            explicit_disc = folder_disc is not None or bool(
                re.match(r"^(?:\d{1,2}[-_.]\d{1,3}|0\d{3})\b", self.base_filename),
            )
            self._position = (disc if explicit_disc else None, number)
        return self._position

    def artist_prefix(self) -> str:
        return filename_artist_prefix(self.base_filename) if self.is_audio else ""


class CandidateDir:
    """Pre-parsed, indexed representation of a discovered remote peer directory."""
    __slots__ = (
        "user", "dir_name", "clean_dir", "dir_words", "dir_sig_words",
        "audio_files", "all_dir_files", "all_file_sig_words", "has_artwork", "dir_info",
        "path_words", "_context",
    )

    def __init__(self, user: str, dir_name: str, dir_info: Dict[str, Any]):
        self.user = user
        self.dir_name = dir_name
        self.dir_info = dir_info
        self.clean_dir = clean_tokens(dir_name)
        self.path_words = _tokenize_words_cached(dir_name)
        self.dir_words = set(self.path_words)
        self.dir_sig_words = {w for w in self.dir_words if len(w) >= 3 and w not in DIR_STOP_WORDS}
        self._context: Dict[FrozenSet[str], bool] = {}

        raw_files = dir_info.get("full_directory_files") or dir_info.get("matched_search_files", [])
        self.audio_files: List[CandidateFile] = []
        self.all_dir_files: List[CandidateFile] = []
        self.all_file_sig_words: Set[str] = set()
        self.has_artwork = False

        seen_files: Set[str] = set()
        for rf in raw_files:
            fn = (rf.get("filename") or rf.get("full_filename") or "").replace("/", "\\").strip().lower()
            if fn and fn in seen_files:
                continue
            if fn:
                seen_files.add(fn)
            cf = CandidateFile(user, dir_name, rf, dir_info)
            self.all_dir_files.append(cf)
            if cf.is_audio:
                self.audio_files.append(cf)
                self.all_file_sig_words.update(cf.sig_words)
            elif cf.is_supporting:
                self.has_artwork = True

    @property
    def disc_dirs(self) -> List[str]:
        """The real folders behind this directory: its disc folders when merged, else itself."""
        return list(self.dir_info.get("disc_dirs") or [self.dir_name])


def file_has_artist_context(cf: CandidateFile, artist_aliases: Iterable[str]) -> bool:
    """A folder of the path, the filename prefix, or the tags credit the artist.

    A folder named after someone whose name merely contains the artist's
    words ('Lone Wolf', 'K-Lone') is no evidence.
    """
    aliases = frozenset(alias for alias in (artist_aliases or ()) if alias)
    alias_lists = _alias_word_lists(aliases)
    if not alias_lists:
        return False
    if path_names_artist(cf.folder, aliases):
        return True
    prefix = cf.artist_prefix()
    if prefix and _names_artist(prefix, aliases):
        return True
    for field in ("artist", "album_artist"):
        tag = cf.raw_file.get(field)
        if tag and _names_artist(str(tag), aliases):
            return True
    return False


def directory_has_artist_context(cd: CandidateDir, artist_aliases: Iterable[str]) -> bool:
    """Whether the folder path or any of its audio files names the artist."""
    aliases = frozenset(alias for alias in (artist_aliases or ()) if alias)
    if aliases not in cd._context:
        alias_lists = _alias_word_lists(aliases)
        cd._context[aliases] = bool(alias_lists) and (
            path_names_artist(cd.dir_name, aliases)
            or any(file_has_artist_context(cf, aliases) for cf in cd.audio_files)
        )
    return cd._context[aliases]


def names_other_artist(cf: CandidateFile, artist_aliases: Iterable[str], track_title: str = "",
                       rel_title: str = "") -> bool:
    """Whether the filename credits someone else ('Other Band - Title'), with no artist tag for us."""
    aliases = frozenset(alias for alias in (artist_aliases or ()) if alias)
    prefix = cf.artist_prefix()
    if not prefix or is_various_artists(prefix) or _names_artist(prefix, aliases):
        return False
    norm_prefix = normalize_text(prefix)
    for title in (track_title, rel_title):
        norm_title = normalize_text(title)
        if norm_title and (norm_prefix == norm_title or norm_title.startswith(norm_prefix + " ")):
            return False
    return not any(
        cf.raw_file.get(field) and _names_artist(str(cf.raw_file[field]), aliases)
        for field in ("artist", "album_artist")
    )


def _significant_words(title: str) -> List[str]:
    return [w for w in _tokenize_words_cached(title or "") if w not in DIR_STOP_WORDS]


def release_context(dir_path: str, rel_title: str) -> bool:
    """The release title's words appear in the folder path.

    Edition and format words are left out on both sides, so 'Ambivert Tools,
    Volume One' names 'Ambivert Tools Volume One' and 'Ambivert Tools Vol One'.
    """
    rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
    if not rel_words:
        return False
    if _has_run([w for w in _tokenize_words_cached(dir_path) if w not in DIR_STOP_WORDS], rel_words):
        return True
    # 'Phonetic Dreams' in 'PhoneticDreams_FLAC'; short titles need whole words.
    compact = "".join(rel_words)
    return len(compact) >= 6 and compact in clean_tokens(dir_path)


class PeerCandidateIndex:
    """In-memory inverted index over discovered Soulseek directories and audio files.

    Per-disc folders of one release ('Album\\CD1', 'Album\\CD2') are also
    offered as one merged directory, so a multi-disc release can be verified
    against a single peer copy.
    """

    def __init__(self, peer_directories: Dict[Tuple[str, str], Dict[str, Any]]):
        self.peer_directories = peer_directories
        self.dirs_map: Dict[Tuple[str, str], CandidateDir] = {}
        self.word_to_dirs: Dict[str, List[CandidateDir]] = {}
        self.word_to_audio_files: Dict[str, List[CandidateFile]] = {}
        self.base_to_audio_files: Dict[str, List[CandidateFile]] = {}
        self.all_audio_files: List[CandidateFile] = []
        self.all_dirs: List[CandidateDir] = []
        # Merged multi-disc releases, keyed by (user, release folder).
        self.disc_groups: Dict[Tuple[str, str], CandidateDir] = {}
        self._group_members: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
        self._build_index()

    def _build_index(self) -> None:
        for (user, dir_name), dir_info in self.peer_directories.items():
            self._add_dir(CandidateDir(user, dir_name, dir_info))
        for (user, dir_name) in list(self.dirs_map):
            group = disc_folder_group(dir_name)
            if group:
                self._group_members.setdefault((user, group), []).append((user, dir_name))
        for group_key in list(self._group_members):
            self._rebuild_group(group_key)

    def _add_dir(self, cd: CandidateDir) -> None:
        self.dirs_map[(cd.user, cd.dir_name)] = cd
        self.all_dirs.append(cd)
        for w in cd.dir_sig_words:
            self.word_to_dirs.setdefault(w, []).append(cd)
        for cf in cd.audio_files:
            self.all_audio_files.append(cf)
            for w in cf.sig_words:
                self.word_to_audio_files.setdefault(w, []).append(cf)
            if cf.p_struct and cf.p_struct["base_norm"]:
                self.base_to_audio_files.setdefault(cf.p_struct["base_norm"], []).append(cf)

    def _remove_dir(self, cd: CandidateDir) -> None:
        self.all_dirs = [item for item in self.all_dirs if item is not cd]
        for w in cd.dir_sig_words:
            if w in self.word_to_dirs:
                self.word_to_dirs[w] = [item for item in self.word_to_dirs[w] if item is not cd]
        if not cd.audio_files or cd.dir_info.get("merged"):
            return
        removed = {id(cf) for cf in cd.audio_files}
        self.all_audio_files = [cf for cf in self.all_audio_files if id(cf) not in removed]
        for cf in cd.audio_files:
            for w in cf.sig_words:
                if w in self.word_to_audio_files:
                    self.word_to_audio_files[w] = [item for item in self.word_to_audio_files[w] if item is not cf]
            base = cf.p_struct["base_norm"] if cf.p_struct else ""
            if base in self.base_to_audio_files:
                self.base_to_audio_files[base] = [item for item in self.base_to_audio_files[base] if item is not cf]

    def _rebuild_group(self, group_key: Tuple[str, str]) -> None:
        old = self.disc_groups.pop(group_key, None)
        if old is not None:
            self._remove_dir(old)
        members = [self.dirs_map[key] for key in self._group_members.get(group_key, []) if key in self.dirs_map]
        parent = self.dirs_map.get(group_key)
        if parent is not None and parent.audio_files:
            members.insert(0, parent)
        if len(members) < 2:
            return
        members.sort(key=lambda cd: cd.dir_name.casefold())
        first = members[0].dir_info
        files: List[Dict[str, Any]] = []
        for member in members:
            files.extend(member.dir_info.get("full_directory_files") or member.dir_info.get("matched_search_files") or [])
        merged = CandidateDir(group_key[0], group_key[1], {
            "user": group_key[0],
            "directory": group_key[1],
            "speed": first.get("speed", 0),
            "queue": first.get("queue", 0),
            "has_slot": first.get("has_slot", True),
            "matched_search_files": files,
            "full_directory_files": None,
            "disc_dirs": [member.dir_name for member in members],
            "merged": True,
        })
        self.disc_groups[group_key] = merged
        self.all_dirs.append(merged)
        for w in merged.dir_sig_words:
            self.word_to_dirs.setdefault(w, []).append(merged)

    def update_directory(self, user: str, dir_name: str, dir_info: Dict[str, Any]) -> CandidateDir:
        """Replace a directory (for example after browsing it) everywhere it is indexed."""
        old = self.dirs_map.get((user, dir_name))
        if old is not None:
            self._remove_dir(old)
        cd = CandidateDir(user, dir_name, dir_info)
        self._add_dir(cd)
        group = disc_folder_group(dir_name)
        if group:
            members = self._group_members.setdefault((user, group), [])
            if (user, dir_name) not in members:
                members.append((user, dir_name))
            self._rebuild_group((user, group))
        if (user, dir_name) in self._group_members:
            # A release folder's own files belong to its merged discs too.
            self._rebuild_group((user, dir_name))
        return cd

    def group_for(self, user: str, dir_name: str) -> Optional[CandidateDir]:
        """The merged multi-disc directory that contains this folder, if any."""
        group = disc_folder_group(dir_name)
        return self.disc_groups.get((user, group)) if group else self.disc_groups.get((user, dir_name))

    def files_with_base(self, base_norm: str) -> List[CandidateFile]:
        return list(self.base_to_audio_files.get(base_norm, ())) if base_norm else []

    def get_candidate_dirs_for_release(
        self,
        rel_title: str,
        parsed_expected: List[Dict[str, Any]],
        expected_count: int
    ) -> List[CandidateDir]:
        clean_rel = clean_tokens(rel_title)
        rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
        rel_sig_words = {w for w in rel_words if len(w) >= 3}

        album_sig_words: Set[str] = set()
        for pe in parsed_expected:
            album_sig_words.update(pe["sig_words"])

        cand_dirs_set: Dict[int, CandidateDir] = {}

        for w in rel_sig_words:
            for cd in self.word_to_dirs.get(w, ()):
                cand_dirs_set[id(cd)] = cd

        if clean_rel:
            for cd in self.all_dirs:
                if clean_rel in cd.clean_dir or cd.clean_dir in clean_rel:
                    cand_dirs_set[id(cd)] = cd

        if expected_count >= 3:
            for w in album_sig_words:
                for cd in self.word_to_dirs.get(w, ()):
                    cand_dirs_set[id(cd)] = cd

        # A disc folder brings in the whole multi-disc release it belongs to.
        for cd in list(cand_dirs_set.values()):
            group = self.group_for(cd.user, cd.dir_name)
            if group is not None:
                cand_dirs_set[id(group)] = group

        return sorted(cand_dirs_set.values(), key=lambda cd: (cd.user.casefold(), cd.dir_name.casefold()))

    def get_candidate_files_for_track(self, parsed_track: Dict[str, Any]) -> List[CandidateFile]:
        sig_words = parsed_track["sig_words"]
        if not sig_words:
            return self.all_audio_files

        matched_files_set: Set[CandidateFile] = set()
        for w in sig_words:
            if w in self.word_to_audio_files:
                matched_files_set.update(self.word_to_audio_files[w])

        return list(matched_files_set) if matched_files_set else self.all_audio_files


def pre_parse_single_track(track_title: str) -> Dict[str, Any]:
    """Pre-parses and tokenizes a MusicBrainz or tag title for fast matching.

    The title is parsed as a title, not a filename: leading numbers ('2 Is 8')
    and ' - ' segments belong to it.
    """
    p_struct = parse_track_title_structure(track_title, is_filename=False)
    clean_base = p_struct["base_norm"] or normalize_text(track_title)
    words = list(_tokenize_words_cached(p_struct["base_norm"] or track_title))
    clean_words = list(_tokenize_words_cached(clean_base)) or words
    sig_words = {w for w in clean_words if len(w) >= 3 and w not in DIR_STOP_WORDS}
    concat = "".join(clean_words)

    core_title = re.sub(r"\[.*?\]|\(.*?\)", " ", track_title or "").strip()
    core_base = parse_track_title_structure(core_title, is_filename=False)["base_norm"] if core_title else ""
    core_words = list(_tokenize_words_cached(core_base))
    core_concat = "".join(core_words)
    core_sig_words = {w for w in core_words if len(w) >= 3 and w not in DIR_STOP_WORDS}

    return {
        "title": track_title,
        "p_struct": p_struct,
        "clean_base": clean_base,
        "words": words,
        "clean_words": clean_words,
        "sig_words": sig_words,
        "concat": concat,
        "core_base": core_base,
        "core_words": core_words,
        "core_sig_words": core_sig_words,
        "core_concat": core_concat,
    }


def pre_parse_expected_tracks(expected_tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [pre_parse_single_track(t.get("title", "")) for t in expected_tracks]


def _covers_file_title(expected: Sequence[str], file_words: Sequence[str], noise: Set[str]) -> bool:
    """The expected words appear in the file title and make up most of what is not noise.

    'Paper Moon' does not take '01 Paper Moon Song', and 'Afterglow' does not
    take 'Eternal Afterglow'; 'Neon Memory' still matches 'Target Artist Neon Memory'.
    """
    if not expected or not _has_run(file_words, expected):
        return False
    expected_set = set(expected)
    core = [w for w in file_words if w in expected_set or w not in noise]
    return len(expected) >= 0.75 * len(core)


def track_match_tier(
    p_exp: Dict[str, Any],
    exp_words: List[str],
    clean_exp_words: List[str],
    exp_sig_words: Set[str],
    exp_concat: str,
    cand: CandidateFile,
    artist_aliases: Iterable[str] = (),
    rel_title: str = "",
) -> int:
    """How closely a remote file's title matches an expected title.

    3: the same base title (any reading of the filename); 2: the expected
    words make up most of the file title; 1: a close spelling (similarity
    >= 0.88 sharing a significant word); 0: no match.
    """
    p_cand = cand.p_struct
    if not p_cand:
        return 0

    struct_exp = p_exp["p_struct"] if "p_struct" in p_exp else p_exp
    if not are_versions_compatible(struct_exp["version_type"], struct_exp["version_text"], p_cand["version_type"], p_cand["version_text"]):
        return 0

    exp_base = struct_exp.get("base_norm") or ""
    core_concat = p_exp.get("core_concat", "")
    if exp_base and (exp_base == p_cand["base_norm"] or exp_base in cand.title_bases()):
        return 3
    if len(exp_concat) >= 3 and exp_concat == cand.concat:
        return 3
    if core_concat and len(core_concat) >= 3 and core_concat == cand.concat:
        return 3

    v_exp = exp_base.split()[-1] if exp_base else ""
    v_cand = cand.clean_base.split()[-1] if cand.clean_base else ""
    if v_exp.isdigit() and v_cand.isdigit() and v_exp != v_cand:
        return 0
    exp_numbers = _title_numbers(exp_base)
    cand_numbers = _title_numbers(cand.clean_base)
    if exp_numbers and cand_numbers and exp_numbers != cand_numbers:
        return 0
    if not exp_words:
        return 0

    noise = {w for run in alias_word_lists(artist_aliases) for w in run}
    noise.update(_tokenize_words_cached(rel_title))
    core_exp_words = p_exp.get("core_words", [])
    expected_runs = [run for run in (clean_exp_words, exp_words, core_exp_words) if run]
    file_runs = [run for run in (cand.clean_words, cand.words) if run]
    if any(_covers_file_title(exp, words, noise) for exp in expected_runs for words in file_runs):
        return 2

    if len(exp_concat) >= 4 and len(cand.concat) >= 4:
        if (exp_sig_words & cand.sig_words) or not exp_sig_words:
            if calculate_similarity(exp_base, cand.clean_base) >= 0.88:
                return 1
    return 0


def same_version_text(expected: Dict[str, Any], candidate: Optional[Dict[str, Any]]) -> bool:
    """Both titles name the same version: 'GUNSHOTTA (VIP)' is not "Gunshotta (Om Unit's Rollers VIP)".

    Spelling and spacing may differ ('Machine Drum remix', 'Machinedrum Remix');
    a title without version text leaves the comparison to are_versions_compatible.
    """
    first = (expected.get("version_text") or "").replace(" ", "")
    second = ((candidate or {}).get("version_text") or "").replace(" ", "")
    if not first or not second:
        return True
    return first == second or calculate_similarity(first, second) >= 0.85


def _has_short_title_context(chk_concat: str, cand: CandidateFile, artist_aliases: Iterable[str],
                             rel_title: str, dir_path: str) -> bool:
    if len(chk_concat) > 3 and chk_concat not in DIR_STOP_WORDS:
        return True
    if file_has_artist_context(cand, artist_aliases):
        return True
    alias_lists = alias_word_lists(artist_aliases)
    if any(_has_run(_tokenize_words_cached(dir_path), run) for run in alias_lists):
        return True
    if rel_title:
        rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
        dir_words = _tokenize_words_cached(dir_path)
        if rel_words and (
            _has_run(dir_words, rel_words[:3]) or any(w in dir_words for w in rel_words if len(w) >= 4)
        ):
            return True
    return False


def has_track_context(cand: CandidateFile, artist_aliases: Iterable[str], track_title: str = "",
                      rel_title: str = "") -> bool:
    """Evidence outside the title that a file is this artist's (or this release's) track.

    Any title needs an artist name in the folder path, filename prefix or tags,
    or the release title in the folder path. A filename credited to another
    artist ('Cymatics - Cobra') never qualifies, and a release titled like the
    track itself (a single) adds nothing the title did not already say.
    """
    if names_other_artist(cand, artist_aliases, track_title, rel_title):
        return False
    if file_has_artist_context(cand, artist_aliases):
        return True
    if not rel_title or not release_context(cand.folder, rel_title):
        return False
    if track_title and _significant_words(track_title) == _significant_words(rel_title):
        return False
    named = directory_named_artist(cand.folder)
    aliases = frozenset(alias for alias in (artist_aliases or ()) if alias)
    return not named or is_various_artists(named) or _names_artist(named, aliases) or (
        normalize_text(named) == normalize_text(rel_title)
    )


def is_track_title_match_fast(
    p_exp: Dict[str, Any],
    exp_words: List[str],
    clean_exp_words: List[str],
    exp_sig_words: Set[str],
    exp_concat: str,
    cand: CandidateFile,
    artist_aliases: Set[str],
    rel_title: str = "",
    dir_path: str = "",
    require_context: bool = False,
) -> bool:
    """Fast track title matcher; require_context also demands artist or release evidence."""
    if not track_match_tier(p_exp, exp_words, clean_exp_words, exp_sig_words, exp_concat, cand,
                            artist_aliases, rel_title):
        return False
    core_concat = p_exp.get("core_concat", "")
    if not _has_short_title_context(core_concat or exp_concat, cand, artist_aliases, rel_title, dir_path):
        return False
    if require_context and not has_track_context(
        cand, artist_aliases, p_exp.get("title") or p_exp.get("raw", ""), rel_title,
    ):
        return False
    return True


def is_dir_name_match_fast(clean_rel: str, rel_sig_words: Set[str], cd: CandidateDir) -> bool:
    if clean_rel and (clean_rel in cd.clean_dir or cd.clean_dir in clean_rel):
        return True
    if not rel_sig_words:
        return False
    matches = len(rel_sig_words & cd.dir_sig_words)
    return matches >= min(2, len(rel_sig_words))


def _track_matches(
    index: PeerCandidateIndex,
    track_title: str,
    artist_aliases: Set[str],
    rel_title: str = "",
    excluded_users: Optional[Set[str]] = None,
    excluded_sources: Optional[Set[Tuple[str, str]]] = None,
    require_context: bool = False,
    exact: bool = False,
) -> List[Tuple[CandidateFile, int]]:
    parsed = pre_parse_single_track(track_title)
    excluded_peer_keys = {peer_key(user) for user in (excluded_users or ())}
    candidates = index.get_candidate_files_for_track(parsed)
    if exact:
        seen = {id(candidate) for candidate in candidates}
        candidates = list(candidates) + [
            candidate for candidate in index.files_with_base(parsed["p_struct"]["base_norm"])
            if id(candidate) not in seen
        ]
    matches = []
    for candidate in candidates:
        if peer_key(candidate.user) in excluded_peer_keys:
            continue
        if excluded_sources and source_key(candidate.user, candidate.full_filename) in excluded_sources:
            continue
        tier = track_match_tier(
            parsed, parsed["words"], parsed["clean_words"], parsed["sig_words"], parsed["concat"],
            candidate, artist_aliases, rel_title,
        )
        if not tier or (exact and (tier < 3 or not same_version_text(parsed["p_struct"], candidate.p_struct))):
            continue
        if not _has_short_title_context(parsed["core_concat"] or parsed["concat"], candidate,
                                        artist_aliases, rel_title, candidate.dir_name):
            continue
        if require_context and not has_track_context(candidate, artist_aliases, track_title, rel_title):
            continue
        matches.append((candidate, tier))
    return matches


def find_track_candidates(
    index: PeerCandidateIndex,
    track_title: str,
    artist_aliases: Set[str],
    rel_title: str = "",
    excluded_users: Optional[Set[str]] = None,
    excluded_sources: Optional[Set[Tuple[str, str]]] = None,
    require_context: bool = False,
    exact: bool = False,
) -> List[CandidateFile]:
    """Return compatible files, preserving source identities for recovery.

    require_context demands artist (or release folder) evidence outside the
    title, and exact demands the same base title; use both for tracks that
    have no release folder to vouch for them.
    """
    return [
        candidate for candidate, _ in _track_matches(
            index, track_title, artist_aliases, rel_title, excluded_users, excluded_sources,
            require_context, exact,
        )
    ]


def candidate_rank_score(candidate: CandidateFile, preferred_format: str = "flac") -> float:
    """Format preference against the peer's queue length."""
    return (
        candidate.fmt_score
        + (25 if preferred_format == "flac" and "FLAC" in candidate.fmt_label else 0)
        - min(candidate.dir_info.get("queue", 0) / 2, 40)
    )


def rank_track_candidates(
    matches: Iterable[Tuple[CandidateFile, int]],
    artist_aliases: Iterable[str],
    preferred_format: str = "flac",
    rel_title: str = "",
    position: Optional[Tuple[Optional[int], Optional[int]]] = None,
) -> List[CandidateFile]:
    """Best first: closer titles, artist evidence, the release's folder and position, then format and queue."""
    def key(item: Tuple[CandidateFile, int]):
        candidate, tier = item
        same_position = bool(
            position and position[1] is not None and candidate.position()[1] == position[1]
            and (position[0] is None or candidate.position()[0] in (None, position[0]))
        )
        return (
            -tier,
            not file_has_artist_context(candidate, artist_aliases),
            not (rel_title and release_context(candidate.folder, rel_title)),
            not same_position,
            -candidate_rank_score(candidate, preferred_format),
            candidate.user.casefold(),
            candidate.full_filename.casefold(),
        )

    return [candidate for candidate, _ in sorted(matches, key=key)]


def find_ranked_track_candidates(
    index: PeerCandidateIndex,
    track_title: str,
    artist_aliases: Set[str],
    preferred_format: str = "flac",
    rel_title: str = "",
    excluded_users: Optional[Set[str]] = None,
    excluded_sources: Optional[Set[Tuple[str, str]]] = None,
    require_context: bool = False,
    exact: bool = False,
    position: Optional[Tuple[Optional[int], Optional[int]]] = None,
) -> List[CandidateFile]:
    """Compatible files, best first (see rank_track_candidates)."""
    return rank_track_candidates(
        _track_matches(
            index, track_title, artist_aliases, rel_title, excluded_users, excluded_sources,
            require_context, exact,
        ),
        artist_aliases, preferred_format, rel_title, position,
    )


def find_best_track_candidate(
    index: PeerCandidateIndex,
    track_title: str,
    artist_aliases: Set[str],
    preferred_format: str = "flac",
    rel_title: str = "",
    excluded_users: Optional[Set[str]] = None,
    excluded_sources: Optional[Set[Tuple[str, str]]] = None,
    require_context: bool = False,
    exact: bool = False,
) -> Optional[CandidateFile]:
    """Choose the best matching file: closest title, then format preference and peer queue length."""
    matches = _track_matches(
        index, track_title, artist_aliases, rel_title, excluded_users, excluded_sources,
        require_context, exact,
    )
    return max(
        matches,
        key=lambda item: (item[1], candidate_rank_score(item[0], preferred_format)),
        default=(None, 0),
    )[0]


def evaluate_directory(
    cd: CandidateDir,
    parsed_expected: List[Dict[str, Any]],
    expected_tracks: List[Dict[str, Any]],
    rel_title: str,
    artist_aliases: Set[str],
    preferred_format: str = "flac",
    min_match_ratio: float = 0.70,
    track_keys: Optional[Sequence[Hashable]] = None,
    positions: Optional[Sequence[Optional[Tuple[Optional[int], Optional[int]]]]] = None,
) -> Optional[Dict[str, Any]]:
    """Match a release against one directory and score the resulting coverage.

    Each file serves one song: files are assigned to expected tracks by match
    strength (an exact title before a partial one, a matching track number
    before a conflicting one). Expected tracks that share a track_key are one
    song (a remaster listed twice) and share its file. match_ratio counts
    distinct songs. Releases of three songs or fewer need the artist's name in
    the folder or files, and any folder without it ranks lower.
    """
    audio_files = cd.audio_files
    if not audio_files:
        return None

    keys = list(track_keys) if track_keys is not None else list(range(len(expected_tracks)))
    songs = list(dict.fromkeys(keys))
    context = directory_has_artist_context(cd, artist_aliases)
    if artist_aliases and len(songs) <= 3 and not context:
        return None

    pairs: List[Tuple[float, int, int]] = []
    for expected_index, pe in enumerate(parsed_expected):
        position = positions[expected_index] if positions and expected_index < len(positions) else None
        for file_index, cf in enumerate(audio_files):
            tier = track_match_tier(
                pe, pe["words"], pe["clean_words"], pe["sig_words"], pe["concat"], cf, artist_aliases, rel_title,
            )
            if not tier or not _has_short_title_context(
                pe.get("core_concat") or pe["concat"], cf, artist_aliases, rel_title, cd.dir_name,
            ):
                continue
            score = float(tier)
            if position and position[1] is not None:
                disc, number = cf.position()
                if number is not None:
                    same_disc = disc is None or position[0] is None or disc == position[0]
                    score += 0.5 if (number == position[1] and same_disc) else -0.25
            pairs.append((-score, expected_index, file_index))
    pairs.sort()

    song_files: Dict[Hashable, int] = {}
    used_files: Set[int] = set()
    for _, expected_index, file_index in pairs:
        key = keys[expected_index]
        if key in song_files or file_index in used_files:
            continue
        song_files[key] = file_index
        used_files.add(file_index)

    matched_tracks: List[Dict[str, Any]] = []
    unmatched_expected: List[Dict[str, Any]] = []
    for expected_index, exp in enumerate(expected_tracks):
        file_index = song_files.get(keys[expected_index])
        if file_index is None:
            unmatched_expected.append(exp)
            continue
        matched_file = audio_files[file_index]
        matched_tracks.append({
            "expected": exp.get("title", ""),
            "expected_index": expected_index,
            "matched_file": matched_file.base_filename,
            "full_filename": matched_file.full_filename,
            "size": matched_file.size
        })

    if not matched_tracks:
        return None
    match_ratio = len(song_files) / len(songs) if songs else 0.0

    primary_audio = audio_files[0]
    format_label, format_score = primary_audio.fmt_label, primary_audio.fmt_score
    queue = cd.dir_info.get("queue", 0)
    speed = cd.dir_info.get("speed", 0)
    has_slot = cd.dir_info.get("has_slot", True)

    total_score = (match_ratio * 100) + format_score + (20 if has_slot else 0) - min(queue / 2, 40)
    if preferred_format == "flac" and "FLAC" in format_label:
        total_score += 25
    if not context:
        total_score -= 40

    return {
        "user": cd.user,
        "directory": cd.dir_name,
        "queue": queue,
        "speed": speed,
        "has_slot": has_slot,
        "format_label": format_label,
        "format_score": format_score,
        "match_ratio": match_ratio,
        "matched_tracks": matched_tracks,
        "unmatched_expected": [u.get("title") for u in unmatched_expected],
        "total_score": total_score,
        "artist_context": context,
        "song_count": len(songs),
        "matched_song_count": len(song_files),
        "disc_dirs": cd.disc_dirs,
        "dir_info": cd.dir_info,
        "all_dir_files": [cf.raw_file for cf in cd.all_dir_files]
    }
