"""Index, match, and rank discovered Soulseek files without network or workflow state."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from musicscraper.core.audio import AudioQualityAnalyzer
from musicscraper.core.constants import AUDIO_EXTENSIONS, DIR_STOP_WORDS, SUPPORTING_EXTENSIONS
from musicscraper.core.text import (
    _tokenize_words_cached,
    are_versions_compatible,
    calculate_similarity,
    clean_tokens,
    extract_dir_and_filename,
    is_sublist,
    parse_track_title_structure,
    strip_track_number_and_artist,
)


class CandidateFile:
    """Pre-parsed, indexed representation of a discovered remote Soulseek file."""
    __slots__ = (
        "user", "dir_name", "raw_file", "dir_info", "base_filename", "full_filename",
        "clean_base", "words", "clean_words", "sig_words", "concat",
        "p_struct", "is_audio", "is_supporting", "fmt_label", "fmt_score", "size"
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


class CandidateDir:
    """Pre-parsed, indexed representation of a discovered remote peer directory."""
    __slots__ = (
        "user", "dir_name", "clean_dir", "dir_words", "dir_sig_words",
        "audio_files", "all_dir_files", "all_file_sig_words", "has_artwork", "dir_info"
    )

    def __init__(self, user: str, dir_name: str, dir_info: Dict[str, Any]):
        self.user = user
        self.dir_name = dir_name
        self.dir_info = dir_info
        self.clean_dir = clean_tokens(dir_name)
        self.dir_words = set(_tokenize_words_cached(dir_name))
        self.dir_sig_words = {w for w in self.dir_words if len(w) >= 3 and w not in DIR_STOP_WORDS}

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


class PeerCandidateIndex:
    """In-memory inverted index over discovered Soulseek directories and audio files."""

    def __init__(self, peer_directories: Dict[Tuple[str, str], Dict[str, Any]]):
        self.peer_directories = peer_directories
        self.dirs_map: Dict[Tuple[str, str], CandidateDir] = {}
        self.word_to_dirs: Dict[str, List[CandidateDir]] = {}
        self.word_to_audio_files: Dict[str, List[CandidateFile]] = {}
        self.all_audio_files: List[CandidateFile] = []
        self.all_dirs: List[CandidateDir] = []
        self._build_index()

    def _build_index(self) -> None:
        for (user, dir_name), dir_info in self.peer_directories.items():
            cd = CandidateDir(user, dir_name, dir_info)
            self.dirs_map[(user, dir_name)] = cd
            self.all_dirs.append(cd)

            for w in cd.dir_sig_words:
                if w not in self.word_to_dirs:
                    self.word_to_dirs[w] = []
                self.word_to_dirs[w].append(cd)

            for cf in cd.audio_files:
                self.all_audio_files.append(cf)
                for w in cf.sig_words:
                    if w not in self.word_to_audio_files:
                        self.word_to_audio_files[w] = []
                    self.word_to_audio_files[w].append(cf)

    def update_directory(self, user: str, dir_name: str, dir_info: Dict[str, Any]) -> CandidateDir:
        cd = CandidateDir(user, dir_name, dir_info)
        self.dirs_map[(user, dir_name)] = cd
        return cd

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

        cand_dirs_set: Set[CandidateDir] = set()

        for w in rel_sig_words:
            if w in self.word_to_dirs:
                cand_dirs_set.update(self.word_to_dirs[w])

        if clean_rel:
            for cd in self.all_dirs:
                if clean_rel in cd.clean_dir or cd.clean_dir in clean_rel:
                    cand_dirs_set.add(cd)

        if expected_count >= 3:
            for w in album_sig_words:
                if w in self.word_to_dirs:
                    cand_dirs_set.update(self.word_to_dirs[w])

        return list(cand_dirs_set)

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
    """Pre-parses and tokenizes a single track title for zero-allocation fast matching."""
    p_struct = parse_track_title_structure(track_title)
    clean_base = p_struct["base_norm"] or strip_track_number_and_artist(track_title)
    words = list(_tokenize_words_cached(p_struct["base_norm"] or track_title))
    clean_words = list(_tokenize_words_cached(clean_base)) or words
    sig_words = {w for w in clean_words if len(w) >= 3 and w not in DIR_STOP_WORDS}
    concat = "".join(clean_words)

    core_title = re.sub(r"\[.*?\]|\(.*?\)", " ", track_title).strip()
    core_base = strip_track_number_and_artist(core_title)
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


def is_track_title_match_fast(
    p_exp: Dict[str, Any],
    exp_words: List[str],
    clean_exp_words: List[str],
    exp_sig_words: Set[str],
    exp_concat: str,
    cand: CandidateFile,
    artist_aliases: Set[str],
    rel_title: str = "",
    dir_path: str = ""
) -> bool:
    """Fast, zero-allocation track title matcher."""
    p_cand = cand.p_struct
    if not p_cand:
        return False

    struct_exp = p_exp["p_struct"] if "p_struct" in p_exp else p_exp
    if not are_versions_compatible(struct_exp["version_type"], struct_exp["version_text"], p_cand["version_type"], p_cand["version_text"]):
        return False

    v_exp = struct_exp["base_norm"].split()[-1] if struct_exp.get("base_norm") else ""
    v_cand = cand.clean_base.split()[-1] if cand.clean_base else ""
    if v_exp.isdigit() and v_cand.isdigit() and v_exp != v_cand:
        return False

    if not exp_words:
        return False

    file_words = cand.words
    clean_file_words = cand.clean_words
    core_exp_words = p_exp.get("core_words", [])
    core_concat = p_exp.get("core_concat", "")

    exact_match = (
        is_sublist(clean_exp_words, clean_file_words) or
        is_sublist(clean_exp_words, file_words) or
        is_sublist(exp_words, clean_file_words) or
        is_sublist(exp_words, file_words) or
        (core_exp_words and (
            is_sublist(core_exp_words, clean_file_words) or
            is_sublist(core_exp_words, file_words)
        ))
    )

    if not exact_match:
        if len(exp_concat) >= 3 and (exp_concat == cand.concat or (len(exp_concat) >= 4 and exp_concat in cand.concat)):
            exact_match = True
        elif core_concat and len(core_concat) >= 3 and (core_concat == cand.concat or (len(core_concat) >= 4 and core_concat in cand.concat)):
            exact_match = True

    if not exact_match and len(exp_concat) >= 4 and len(cand.concat) >= 4:
        if (exp_sig_words & cand.sig_words) or not exp_sig_words:
            sim = calculate_similarity(struct_exp["base_norm"], cand.clean_base)
            if sim >= 0.88:
                exact_match = True

    if not exact_match:
        return False

    chk_concat = core_concat or exp_concat
    if len(chk_concat) <= 3 or chk_concat in DIR_STOP_WORDS:
        dir_norm = clean_tokens(dir_path)
        file_norm = cand.concat
        has_artist_context = any(clean_tokens(alias) in dir_norm or clean_tokens(alias) in file_norm for alias in artist_aliases if len(alias) >= 3)
        has_rel_context = False
        if rel_title:
            rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
            if rel_words:
                has_rel_context = (
                    is_sublist(rel_words[:3], list(_tokenize_words_cached(dir_path))) or
                    any(w in dir_norm for w in rel_words if len(w) >= 4)
                )

        if not has_artist_context and not has_rel_context:
            return False

    return True


def is_dir_name_match_fast(clean_rel: str, rel_sig_words: Set[str], cd: CandidateDir) -> bool:
    if clean_rel and (clean_rel in cd.clean_dir or cd.clean_dir in clean_rel):
        return True
    if not rel_sig_words:
        return False
    matches = len(rel_sig_words & cd.dir_sig_words)
    return matches >= min(2, len(rel_sig_words))


def find_best_track_candidate(
    index: PeerCandidateIndex,
    track_title: str,
    artist_aliases: Set[str],
    preferred_format: str = "flac",
    rel_title: str = "",
) -> Optional[CandidateFile]:
    """Choose the best matching file by format preference and peer queue length."""
    parsed = pre_parse_single_track(track_title)
    matches = [
        candidate
        for candidate in index.get_candidate_files_for_track(parsed)
        if is_track_title_match_fast(
            parsed["p_struct"],
            parsed["words"],
            parsed["clean_words"],
            parsed["sig_words"],
            parsed["concat"],
            candidate,
            artist_aliases,
            rel_title,
            candidate.dir_name,
        )
    ]
    return max(
        matches,
        key=lambda candidate: (
            candidate.fmt_score
            + (25 if preferred_format == "flac" and "FLAC" in candidate.fmt_label else 0)
            - min(candidate.dir_info.get("queue", 0) / 2, 40)
        ),
        default=None,
    )


def evaluate_directory(
    cd: CandidateDir,
    parsed_expected: List[Dict[str, Any]],
    expected_tracks: List[Dict[str, Any]],
    rel_title: str,
    artist_aliases: Set[str],
    preferred_format: str = "flac",
    min_match_ratio: float = 0.70,
) -> Optional[Dict[str, Any]]:
    """Match a release against one directory and score the resulting coverage."""
    audio_files = cd.audio_files
    if not audio_files:
        return None

    matched_tracks: List[Dict[str, Any]] = []
    unmatched_expected: List[Dict[str, Any]] = []

    for pe, exp in zip(parsed_expected, expected_tracks):
        exp_title = exp.get("title", "")
        matched_file = None

        for cf in audio_files:
            if is_track_title_match_fast(
                pe["p_struct"], pe["words"], pe["clean_words"], pe["sig_words"], pe["concat"],
                cf, artist_aliases, rel_title, cd.dir_name
            ):
                matched_file = cf
                break

        if matched_file:
            matched_tracks.append({
                "expected": exp_title,
                "matched_file": matched_file.base_filename,
                "full_filename": matched_file.full_filename,
                "size": matched_file.size
            })
        else:
            unmatched_expected.append(exp)

    match_ratio = len(matched_tracks) / len(expected_tracks) if expected_tracks else 0.0
    if match_ratio < min_match_ratio and len(matched_tracks) == 0:
        return None

    primary_audio = audio_files[0]
    format_label, format_score = primary_audio.fmt_label, primary_audio.fmt_score
    queue = cd.dir_info.get("queue", 0)
    speed = cd.dir_info.get("speed", 0)
    has_slot = cd.dir_info.get("has_slot", True)

    total_score = (match_ratio * 100) + format_score + (20 if has_slot else 0) - min(queue / 2, 40)
    if preferred_format == "flac" and "FLAC" in format_label:
        total_score += 25

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
        "dir_info": cd.dir_info,
        "all_dir_files": [cf.raw_file for cf in cd.all_dir_files]
    }
