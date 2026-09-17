"""Coordinate Soulseek artist discovery, reconciliation, and download queueing."""

import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any

from unidecode import unidecode

from discogseek.config import Config
from discogseek.core.constants import (
    DIR_STOP_WORDS,
    GENERIC_OR_COMMON_WORDS,
)
from discogseek.core.text import (
    normalize_text,
    clean_tokens,
    _tokenize_words_cached,
    clean_search_phrase,
    extract_dir_and_filename,
)
from discogseek.clients.slskd import SlskdClient
from discogseek.clients.musicbrainz import MusicBrainzClient, ArtistCatalog
from discogseek.services.auditor import AuditorService
from discogseek.services.candidates import (
    CandidateDir,
    CandidateFile,
    PeerCandidateIndex,
    evaluate_directory,
    find_best_track_candidate,
    is_dir_name_match_fast,
    is_track_title_match_fast,
    pre_parse_expected_tracks,
    pre_parse_single_track,
)
from discogseek.services.reconciler import DiscographyReconciler


# Keep candidate helpers available at their original import path.
__all__ = [
    "CandidateDir",
    "CandidateFile",
    "PeerCandidateIndex",
    "SlskdArtistScraper",
    "is_dir_name_match_fast",
    "is_track_title_match_fast",
    "pre_parse_expected_tracks",
    "pre_parse_single_track",
]


class SlskdArtistScraper:
    """Orchestrates parallel Soulseek discovery, directory expansion, reconciliation, and queueing."""

    def __init__(
        self,
        artist_query: str,
        slskd_client: Optional[SlskdClient] = None,
        music_dir: Optional[Path] = None,
        preferred_format: str = "flac",
        min_match_ratio: float = 0.70,
        search_timeout: float = 30.0,
        dry_run: bool = False,
        threads: int = 6,
        full_scan: bool = False,
        force_refresh: bool = False,
    ):
        self.artist_query = artist_query.strip()
        self.client = slskd_client or SlskdClient()
        self.music_dir = Path(music_dir or Config.DEFAULT_LIBRARY_DIR).resolve() if (music_dir or Config.DEFAULT_LIBRARY_DIR) else None
        self.preferred_format = preferred_format.lower()
        self.min_match_ratio = min_match_ratio
        self.search_timeout = search_timeout
        self.dry_run = dry_run
        self.threads = threads
        self.full_scan = full_scan
        self.force_refresh = force_refresh
        self.enqueued_count = 0
        self.queue_errors: List[str] = []

        self.mb_client = MusicBrainzClient()
        self.catalog: Optional[ArtistCatalog] = None
        self.raw_mb_data: Dict[str, Any] = {}
        self.all_artist_aliases: Set[str] = set()

        self.local_found_map: Dict[str, Dict[str, Any]] = {}
        self.local_found_releases: Set[str] = set()

        self.peer_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.candidate_index: Optional[PeerCandidateIndex] = None
        self.searches_performed: Set[str] = set()

        self.reconciled_release_keys: Set[str] = set()
        self.covered_track_titles: Set[str] = set()

        self.queued_directories: List[Dict[str, Any]] = []
        self.already_downloading_files: Set[str] = set()
        self.verified_releases: List[Dict[str, Any]] = []
        self.unresolved_releases: List[Dict[str, Any]] = []
        self.verified_compilation_tracks: List[Dict[str, Any]] = []
        self.unresolved_compilation_tracks: List[Dict[str, Any]] = []
        self.verified_standalone_tracks: List[Dict[str, Any]] = []
        self.unresolved_standalone_tracks: List[Dict[str, Any]] = []

    def run(self) -> Dict[str, Any]:
        """Runs the complete Soulseek discography discovery and queueing pipeline."""
        # 1. Check slskd Connection
        app_info = self.client.get_application()
        slsk_user = app_info.get("user", {}).get("username", "Unknown")
        server_state = app_info.get("server", {}).get("state", "Unknown")
        logging.getLogger(__name__).info(f"✔ Connected to slskd (User: {slsk_user} | Server: {server_state})")

        self.already_downloading_files = self.client.get_queued_filenames()

        # 2. Resolve Artist & Catalog
        mbid, canonical_name = self.mb_client.resolve_artist_mbid(self.artist_query)
        self.raw_mb_data = self.mb_client.fetch_full_discography(mbid, force_refresh=self.force_refresh)
        self.catalog = ArtistCatalog(self.raw_mb_data)

        self.all_artist_aliases = set(self.catalog.aliases)
        self.all_artist_aliases.add(self.catalog.name)
        for a in list(self.all_artist_aliases):
            self.all_artist_aliases.add(unidecode(a))

        logging.getLogger(__name__).info(f"✔ Canonical Name: {self.catalog.name} (MBID: {self.catalog.mbid})")
        logging.getLogger(__name__).info(f"Catalog: {len(self.catalog.tracks)} total tracks | {len(self.catalog.primary_releases)} primary releases")

        # 3. Pre-scan local library
        self._prescan_library()

        # 4. Search Soulseek
        self._discover_soulseek_candidates()

        # 5. Reconcile primary releases
        self._reconcile_primary_releases()

        # 6. Reconcile compilations and singles
        self._reconcile_compilation_tracks()
        self._reconcile_standalone_tracks()

        # 7. Queue downloads
        if not self.dry_run:
            self._queue_downloads()
        else:
            logging.getLogger(__name__).info("\n--dry-run enabled: Showing matched directories without enqueuing transfers.")

        return {
            "artist": self.catalog.name,
            "mbid": self.catalog.mbid,
            "queued_directories": self.queued_directories,
            "verified_releases": self.verified_releases,
            "verified_compilation_tracks": self.verified_compilation_tracks,
            "verified_standalone_tracks": self.verified_standalone_tracks,
            "unresolved_releases": self.unresolved_releases,
            "unresolved_compilation_tracks": self.unresolved_compilation_tracks,
            "unresolved_standalone_tracks": self.unresolved_standalone_tracks,
            "enqueued_count": self.enqueued_count,
            "queue_errors": self.queue_errors,
            "dry_run": self.dry_run,
        }

    def _prescan_library(self) -> None:
        local_tracks = AuditorService(mb_client=self.mb_client).scan_library(
            self.catalog, self.music_dir, full_scan=self.full_scan, threads=self.threads,
        )
        if not local_tracks:
            return

        reconciler = DiscographyReconciler(catalog=self.catalog, local_tracks=local_tracks)
        found_items, _ = reconciler.reconcile()

        for item in found_items:
            mb = item["mb_track"]
            norm_t = mb.get("norm_title", "")
            if norm_t:
                self.local_found_map[norm_t] = item["local_track"]

        self.covered_track_titles.update(self.local_found_map)

        for rel in self.catalog.releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            rel_tracks = [
                t for t in self.catalog.tracks
                if norm_rel in [normalize_text(r) for r in t.get("all_releases", set())]
                or t.get("norm_release") == norm_rel
            ]
            if not rel_tracks:
                continue

            artist_tracks = [
                t for t in rel_tracks
                if any(alias.lower() in t.get("artist_credit", "").lower() for alias in self.all_artist_aliases)
                or t.get("artist_credit", "").lower() == self.catalog.name.lower()
            ]

            found_rel_tracks = [t for t in rel_tracks if t.get("norm_title") in self.local_found_map]
            found_artist_tracks = [t for t in artist_tracks if t.get("norm_title") in self.local_found_map]

            is_complete = False
            if artist_tracks and len(found_artist_tracks) == len(artist_tracks):
                is_complete = True
            elif rel_tracks and len(found_rel_tracks) == len(rel_tracks):
                is_complete = True

            if is_complete:
                self.local_found_releases.add(norm_rel)
                self.reconciled_release_keys.add(norm_rel)

        logging.getLogger(__name__).info(f"✔ Library Status: {len(found_items)} artist tracks / {len(self.local_found_releases)} releases already in library.")

    def _generate_all_search_queries(self) -> List[str]:
        queries: List[str] = []

        # 1. Primary Artist Names (User query + Canonical MB Name)
        if self.artist_query:
            q_user = clean_search_phrase(self.artist_query)
            if q_user and q_user not in queries:
                queries.append(q_user)

        if self.catalog and self.catalog.name:
            q_name = clean_search_phrase(self.catalog.name)
            if q_name and q_name not in queries:
                queries.append(q_name)

        # 2. Targeted Primary Release Queries (User Query + Release, Canonical Name + Release, Release Title)
        if self.catalog:
            for rel in self.catalog.primary_releases:
                rel_title = rel.get("title", "")
                norm_rel = normalize_text(rel_title)
                if not rel_title or norm_rel in self.local_found_releases:
                    continue

                clean_rel = clean_search_phrase(rel_title)

                # Query with user artist query (e.g. "Stellabee Breakcore Forever")
                if self.artist_query:
                    q_user_rel = clean_search_phrase(f"{self.artist_query} {rel_title}")
                    if q_user_rel and q_user_rel not in queries:
                        queries.append(q_user_rel)

                # Query with canonical name (e.g. "すてらべえ Breakcore Forever")
                if self.catalog.name and self.catalog.name.lower() != (self.artist_query or "").lower():
                    q_canon_rel = clean_search_phrase(f"{self.catalog.name} {rel_title}")
                    if q_canon_rel and q_canon_rel not in queries:
                        queries.append(q_canon_rel)

                # If the release title is distinctive (len >= 4 and not generic noise), add release title query
                if clean_rel and len(clean_rel) >= 4 and clean_rel.lower() not in DIR_STOP_WORDS and clean_rel.lower() not in GENERIC_OR_COMMON_WORDS:
                    if clean_rel not in queries:
                        queries.append(clean_rel)

        # 3. Targeted Compilation Release Queries (e.g. "Various Artists Amen Destroyer")
        if self.catalog:
            for rel in self.catalog.compilation_releases:
                rel_title = rel.get("title", "")
                norm_rel = normalize_text(rel_title)
                if not rel_title or norm_rel in self.local_found_releases:
                    continue
                clean_rel = clean_search_phrase(rel_title)
                if clean_rel and len(clean_rel) >= 4 and clean_rel.lower() not in DIR_STOP_WORDS and clean_rel.lower() not in GENERIC_OR_COMMON_WORDS:
                    q_va_rel = clean_search_phrase(f"Various Artists {rel_title}")
                    if q_va_rel and q_va_rel not in queries:
                        queries.append(q_va_rel)

        # 4. Artist Aliases (Ordered by relevance, skipping overly short/symbolic noise)
        if self.catalog:
            for alias in self.catalog.aliases:
                alias_clean = clean_search_phrase(alias)
                if alias_clean and len(alias_clean) >= 3 and alias_clean.lower() != (self.artist_query or "").lower() and alias_clean.lower() != self.catalog.name.lower():
                    if alias_clean not in queries:
                        queries.append(alias_clean)

        return list(dict.fromkeys(q for q in queries if q and len(q) >= 3))

    def _discover_soulseek_candidates(self) -> None:
        all_queries = self._generate_all_search_queries()
        logging.getLogger(__name__).info(f"\nExecuting parallel Soulseek searches ({len(all_queries)} targeted queries)...")

        batch_results = self.client.batch_search(
            all_queries, timeout=self.search_timeout, poll_interval=1.0,
        )
        total_files = 0
        for query_str, s_data in batch_results.items():
            for resp in s_data.get("responses", []):
                user = resp.get("username")
                speed = resp.get("uploadSpeed", 0)
                queue = resp.get("queueLength", 0)
                has_slot = resp.get("hasFreeUploadSlot", True)
                for f in resp.get("files", []):
                    fn = f.get("filename", "")
                    if not fn or f.get("isLocked", False):
                        continue
                    dir_name, _ = extract_dir_and_filename(fn)
                    if not dir_name:
                        continue
                    key = (user, dir_name)
                    if key not in self.peer_directories:
                        self.peer_directories[key] = {
                            "user": user,
                            "directory": dir_name,
                            "speed": speed,
                            "queue": queue,
                            "has_slot": has_slot,
                            "matched_search_files": [],
                            "full_directory_files": None,
                            "_seen_files": set()
                        }
                    fn_norm = fn.strip().lower()
                    if fn_norm not in self.peer_directories[key]["_seen_files"]:
                        self.peer_directories[key]["_seen_files"].add(fn_norm)
                        self.peer_directories[key]["matched_search_files"].append(f)
                        total_files += 1

        logging.getLogger(__name__).info(f"✔ Soulseek Discovery: Found {len(self.peer_directories)} candidate directories ({total_files} candidate files).")
        self.candidate_index = PeerCandidateIndex(self.peer_directories)

    def _evaluate_indexed_directory(
        self,
        cd: CandidateDir,
        parsed_expected: List[Dict[str, Any]],
        expected_tracks: List[Dict[str, Any]],
        rel_title: str
    ) -> Optional[Dict[str, Any]]:
        return evaluate_directory(
            cd,
            parsed_expected,
            expected_tracks,
            rel_title,
            self.all_artist_aliases,
            self.preferred_format,
            self.min_match_ratio,
        )

    def _reconcile_primary_releases(self) -> None:
        primary_rels = self.catalog.primary_releases
        logging.getLogger(__name__).info(f"\nVerifying Primary Releases ({len(primary_rels)} releases)...")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for rel in primary_rels:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            if norm_rel in self.local_found_releases or norm_rel in self.reconciled_release_keys:
                continue
            self.reconciled_release_keys.add(norm_rel)

            expected_tracks = [
                t for t in self.catalog.tracks
                if norm_rel in [normalize_text(r) for r in t.get("all_releases", set())]
                or t.get("norm_release") == norm_rel
            ]
            if not expected_tracks:
                continue

            parsed_expected = pre_parse_expected_tracks(expected_tracks)
            clean_rel = clean_tokens(rel_title)
            rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
            rel_sig_words = {w for w in rel_words if len(w) >= 3}

            cand_dirs = self.candidate_index.get_candidate_dirs_for_release(rel_title, parsed_expected, len(expected_tracks))
            candidate_matches: List[Dict[str, Any]] = []

            # Phase 1: Evaluate candidate directories with already indexed search files
            dirs_needing_browse: List[CandidateDir] = []
            for cd in cand_dirs:
                if is_dir_name_match_fast(clean_rel, rel_sig_words, cd):
                    if cd.audio_files:
                        eval_res = self._evaluate_indexed_directory(cd, parsed_expected, expected_tracks, rel_title)
                        if eval_res and eval_res["match_ratio"] >= self.min_match_ratio:
                            matched_new_tracks = [
                                m for m in eval_res["matched_tracks"]
                                if normalize_text(m["expected"]) not in self.local_found_map
                            ]
                            if matched_new_tracks or len(eval_res["matched_tracks"]) == 0:
                                candidate_matches.append(eval_res)
                    if (not cd.audio_files or len(cd.audio_files) < len(expected_tracks)) and cd.dir_info.get("full_directory_files") is None:
                        dirs_needing_browse.append(cd)

            # Phase 2: If no candidate matched, batch browse the top candidate directories
            if not candidate_matches and dirs_needing_browse:
                top_to_browse = [(cd.user, cd.dir_name) for cd in dirs_needing_browse[:6]]
                try:
                    browse_res = self.client.browse_directories_batch(top_to_browse, max_workers=6)
                    for (u, d), remote_files in browse_res.items():
                        if remote_files and (u, d) in self.peer_directories:
                            self.peer_directories[(u, d)]["full_directory_files"] = remote_files
                            upd_cd = self.candidate_index.update_directory(u, d, self.peer_directories[(u, d)])
                            if upd_cd.audio_files:
                                eval_res = self._evaluate_indexed_directory(upd_cd, parsed_expected, expected_tracks, rel_title)
                                if eval_res and eval_res["match_ratio"] >= self.min_match_ratio:
                                    candidate_matches.append(eval_res)
                except Exception:
                    pass

            if candidate_matches:
                best = max(candidate_matches, key=lambda x: x["total_score"])
                missing_filenames = {
                    m["full_filename"] for m in best["matched_tracks"]
                    if normalize_text(m["expected"]) not in self.local_found_map
                }
                best["all_dir_files"] = [
                    f for f in best["all_dir_files"] if f.get("filename") in missing_filenames
                ]
                if not best["all_dir_files"]:
                    continue
                self.verified_releases.append({
                    "release": rel_title,
                    "user": best["user"],
                    "directory": best["directory"],
                    "match_ratio": best["match_ratio"],
                    "matched_count": len(best["matched_tracks"]),
                    "total_count": len(expected_tracks),
                    "format_label": best["format_label"],
                    "queue": best["queue"],
                    "speed": best["speed"],
                    "files": best["all_dir_files"]
                })
                self.queued_directories.append(best)
                for m in best["matched_tracks"]:
                    self.covered_track_titles.add(normalize_text(m["expected"]))
                logging.getLogger(__name__).info(f"✔ Matched Album: {rel_title} ({best['format_label']}) from {best['user']} ({len(best['matched_tracks'])}/{len(expected_tracks)} tracks)")
            else:
                self.unresolved_releases.append(rel)

    def _reconcile_compilation_tracks(self) -> None:
        comp_releases = self.catalog.compilation_releases
        if not comp_releases:
            return
        logging.getLogger(__name__).info(f"\nVerifying Compilation / VA Releases ({len(comp_releases)} releases)...")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for rel in comp_releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            if norm_rel in self.local_found_releases or norm_rel in self.reconciled_release_keys:
                continue

            comp_tracks = [
                t for t in self.catalog.tracks
                if (norm_rel in [normalize_text(r) for r in t.get("all_releases", set())]
                or t.get("norm_release") == norm_rel)
                and (any(alias.lower() in t.get("artist_credit", "").lower() for alias in self.all_artist_aliases)
                     or t.get("artist_credit", "").lower() == self.catalog.name.lower()
                     or not t.get("artist_credit"))
            ]
            if not comp_tracks:
                continue

            for t in comp_tracks:
                t_title = t.get("title", "")
                norm_t = normalize_text(t_title)
                if norm_t in self.local_found_map or norm_t in self.covered_track_titles:
                    continue

                best_cf = find_best_track_candidate(
                    self.candidate_index,
                    t_title,
                    self.all_artist_aliases,
                    self.preferred_format,
                    rel_title,
                )
                if best_cf:
                    self.covered_track_titles.add(norm_t)
                    self.verified_compilation_tracks.append({
                        "release": rel_title,
                        "track": t_title,
                        "user": best_cf.user,
                        "file": best_cf.raw_file,
                        "format_label": best_cf.fmt_label,
                    })
                    logging.getLogger(__name__).info(f"✔ Matched VA Track: {t_title} ({best_cf.fmt_label}) from {best_cf.user} (on '{rel_title}')")
                else:
                    self.unresolved_compilation_tracks.append({"release": rel_title, "track": t_title})

    def _reconcile_standalone_tracks(self) -> None:
        standalone = [t for t in self.catalog.tracks if t.get("release_type") == "Standalone / Single"]
        if not standalone:
            return
        logging.getLogger(__name__).info(f"\nVerifying Standalone Tracks ({len(standalone)} tracks)...")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for t in standalone:
            t_title = t.get("title", "")
            norm_t = normalize_text(t_title)
            if norm_t in self.local_found_map or norm_t in self.covered_track_titles:
                continue

            best_cf = find_best_track_candidate(
                self.candidate_index,
                t_title,
                self.all_artist_aliases,
                self.preferred_format,
            )
            if best_cf:
                self.covered_track_titles.add(norm_t)
                self.verified_standalone_tracks.append({
                    "track": t_title,
                    "user": best_cf.user,
                    "file": best_cf.raw_file,
                    "format_label": best_cf.fmt_label,
                })
                logging.getLogger(__name__).info(f"✔ Matched Single: {t_title} ({best_cf.fmt_label}) from {best_cf.user}")
            else:
                self.unresolved_standalone_tracks.append(t)

    def _queue_downloads(self) -> None:
        total_items = len(self.queued_directories) + len(self.verified_compilation_tracks) + len(self.verified_standalone_tracks)
        if total_items == 0:
            logging.getLogger(__name__).info("No releases or tracks to enqueue.")
            return

        try:
            self.already_downloading_files = self.client.get_queued_filenames()
            queued_fps = self.client.get_queued_track_fingerprints()
        except Exception:
            queued_fps = {"base_filenames": set(), "clean_titles": set(), "full_paths": set()}

        if self.queued_directories:
            logging.getLogger(__name__).info(f"\nEnqueuing {len(self.queued_directories)} verified releases into slskd...")
            for d in self.queued_directories:
                try:
                    files_to_download = []
                    for f in d["all_dir_files"]:
                        fn = f.get("filename", "")
                        if not fn:
                            continue
                        clean_p = fn.replace("/", "\\").split("\\")[-1].lower()
                        if fn in self.already_downloading_files or clean_p in queued_fps["base_filenames"]:
                            continue

                        files_to_download.append(f)
                        self.already_downloading_files.add(fn)
                        queued_fps["base_filenames"].add(clean_p)

                    if not files_to_download:
                        logging.getLogger(__name__).info(f"↷ Skipping already queued folder: {d['directory']} from {d['user']}")
                        continue

                    self.client.enqueue_download(d["user"], files_to_download)
                    self.enqueued_count += len(files_to_download)
                    logging.getLogger(__name__).info(f"✔ Enqueued folder: {d['directory']} from {d['user']} ({len(files_to_download)} files)")
                except Exception as e:
                    self.queue_errors.append(f"Failed to enqueue {d['directory']}: {e}")

        # Enqueue individual standalone / compilation tracks
        single_tracks_by_user: Dict[str, List[Dict[str, Any]]] = {}
        for item in self.verified_compilation_tracks + self.verified_standalone_tracks:
            u = item["user"]
            f = item["file"]
            fn = f.get("filename", "")
            if not fn:
                continue
            clean_p = fn.replace("/", "\\").split("\\")[-1].lower()
            if fn in self.already_downloading_files or clean_p in queued_fps["base_filenames"]:
                continue
            if u not in single_tracks_by_user:
                single_tracks_by_user[u] = []
            single_tracks_by_user[u].append(f)
            self.already_downloading_files.add(fn)
            queued_fps["base_filenames"].add(clean_p)

        for u, files in single_tracks_by_user.items():
            try:
                self.client.enqueue_download(u, files)
                self.enqueued_count += len(files)
                logging.getLogger(__name__).info(f"✔ Enqueued {len(files)} standalone/compilation tracks from {u}")
            except Exception as e:
                self.queue_errors.append(f"Failed to enqueue tracks from {u}: {e}")
