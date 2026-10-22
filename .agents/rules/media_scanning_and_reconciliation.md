# Media Library Scanning, Tag Extraction & Reconciliation Rules

Guidelines and invariants for audio file discovery, metadata tag parsing, persistent caching, and MusicBrainz discography reconciliation in the `discogseek` codebase.

## 1. Fast Candidate Discovery & Substring Avoidance
- **Never use naive substring matching** (`needle in string` or `any(x in string for x in list)`) on disk paths or filenames without word boundaries.
- **Word Boundaries**: Always enforce word boundaries (`(?:\b|_)` or tokenization) when matching artist aliases, release names, or track titles.
- **Generic Token & Common Word Exclusion**:
  - Filter out generic track markers (`intro`, `outro`, `interlude`, `untitled`, `bonus`, `track`, `demo`, `mix`, `edit`, `vip`, `theme`, pure digits, single letters).
  - Filter out common single English dictionary words and electronic genre keywords (`breakcore`, `lolicore`, `speedcore`, `hardcore`, `ambient`, `vaporwave`, `rave`) when matching loose standalone filenames.
  - Require standalone track titles to be distinct (e.g. multi-word or length $\ge 8$) unless accompanied by an artist alias or album container match.
  - **Unified Regex Matching**: In high-latency/SSHFS filesystems, compile combined unified regexes for aliases, release titles, and track titles rather than sequentially evaluating hundreds of regexes per file.
- **Compilation Folder Filtering**: Do not blindly classify every audio file in a compilation/VA folder as a candidate for a single queried artist. Only match files in compilation directories if the album title matches a known catalog release or the track filename contains the artist's name.
- **Folder-Level Candidate Propagation**: If a non-canonical folder (such as inside a downloads directory) contains $\ge 2$ tracks matching catalog items, scan the entire folder as a cohesive album candidate.

## 2. Persistent Audio Tag Caching
- **SQLite Metadata Caching**: Always check the audio metadata cache (`core.cache.UnifiedCacheManager`, `$DISCOGSEEK_CACHE_DIR/discogseek_cache.db`, default `~/.cache/discogseek`) before invoking `mutagen` file reads over disk. Tests and probes must point `DISCOGSEEK_CACHE_DIR` at a temporary copy, never at the user's cache.
- **Cache Key Invariants**: Cache records must be keyed by `(path, mtime, size)`. Unmodified files must resolve from cache in microseconds to prevent multi-minute blocking disk I/O.
- **Batch Operations**: Perform SQLite cache lookups and writes in chunks/batches.
- **Extractor-Versioned Rows**: Every cached row stores `extractor_version` (`core.audio.AUDIO_EXTRACTOR_VERSION`). Bump that constant whenever `analyze_file` reads tags differently, and extend `core.cache._is_outdated_audio_row` so that **only** the rows the change affects are re-read (by format, by a missing value, or by NUL-joined legacy values). Never force a full-library re-read: the library sits on a slow network mount.
  - Folders whose cached album artists disagree are re-read when any of their rows comes from an older extractor (`library_scan._legacy_rows_with_inconsistent_album_artists`); a file whose re-read or stat fails keeps its cached tags for that scan.
  - Never cache a failed read (`AudioMetadata.read_error`): its folder/filename fallback is valid for the current scan only.
- **Browser Audit Cache**: Bump `library_browser_cache._NAMESPACE` whenever reconciliation rules or required audit fields change, so saved audits of unchanged files are rebuilt. Release-group links between editions live in their own namespace and are saved even when a scan stops early.

## 3. Safe Audio Tag Extraction (ID3, Vorbis, MP4)
- **Exact ID3 Frame Matching**:
  - Never use loose substring checks like `'ALBUM' in k_str` or `'TITLE' in k_str` on ID3 tag frames.
  - Match exact frame names (`TALB`, `TIT2`, `TRCK`, `TPE1`, `TPE2`, `UFID`, `TXXX:MusicBrainz...`).
  - Prevent ReplayGain peak/gain frames (`TXXX:replaygain_album_peak = '1.000000'`) from overwriting legitimate album or title tags.
- **Vorbis & MP4 Handling**: Standardize case-insensitive vorbis keys (`TITLE`, `ALBUM`, `ARTIST`, `TRACKNUMBER`) and MP4 atoms (`\xa9nam`, `\xa9alb`, `\xa9ART`, `aART`, `trkn`).
- **Fixed Key Precedence**: Choose each field from an explicit, ordered key tuple (`ALBUM_ARTIST_KEYS`, `ARTIST_KEYS`, ...). Never take the first match while iterating mutagen's tag mapping: Vorbis key order follows the process hash seed, so a file with both `ALBUMARTIST` and `ALBUM ARTIST` would change album artist between runs and split albums.
- **Positions and IDs in Every Container**: Read MP4 `trkn`/`disk` `(number, total)` pairs, MP4 freeform MusicBrainz atoms (`----:com.apple.iTunes:MusicBrainz Album Id`, UTF-8 bytes, keyed by their suffix), and APEv2 `Track`. Keep track totals (`n/N`, `TRACKTOTAL`/`TOTALTRACKS`, `trkn`) in `track_total`; the scan uses them to add placeholders for trailing missing tracks.
- **Multi-Value Frames**: Read ID3 frames through `.text` and split APEv2 NUL-separated values; `str(frame)` joins values with NUL and corrupts titles, artists and MBID sets.

## 4. Multi-Tier Reconciliation & False-Positive Prevention
- **Tier 1 (Exact MBID)**: Highest priority match on Track ID, Release Track ID, or Recording UFID.
- **Tier 2 (Exact Release + Title)**: Matches track title and album title/path with $\ge 88\%$ base title similarity and strict version compatibility.
  - A release title in the path must match whole words of a directory component (`_path_names_release`); a title shorter than 3 characters must equal a whole folder name ('On' is not in 'Online Sessions').
  - Path-only release evidence (Tiers 2 and 4) is rejected when the file's own artist credits name only other artists (`_credits_other_artist`), unless the album tag itself is (nearly) the release title. Untagged files are not evidence against the artist.
  - MusicBrainz and tag titles are parsed with `parse_track_title_structure(title, is_filename=False)`; only a raw filename (no title tag) has its track number and artist prefix stripped.
- **Tier 3 (Artist Alias + Title)**: Matches track title ($\ge 90\%$ base title similarity) and verifies that artist alias exists in tags or file path, with strict version compatibility.
- **Tier 4 (Fuzzy Match Guardrails)**:
  - Fuzzy matching must **never** attribute tracks to an artist without container or tag verification.
  - Require artist alias confirmation (`has_artist_tag` / `has_artist_path`) or release album confirmation (`has_rel_match`) before accepting a fuzzy match.
  - Standalone tracks without artist or album confirmation require very high direct similarity ($\ge 92\%$) and non-generic title status.
  - **Strict Version & Remix Incompatibility**:
    - An original track (`version_type is None`) must **never** match a remix, instrumental, acoustic, live, or speed-up version.
    - Remixes with differing remixers/descriptors (e.g. `WilliamDavi's remix` vs `Vaenus remix`) must **never** match each other.
    - Never bypass version compatibility with unvalidated substring checks (`title_in_tag` / `title_in_path`).
    - Distinguish featured artists (`feat.`, `ft.`) from version modifiers; featured artists do not alter the version type.
    - Treat non-musical sound engineering tags (`[2021 Remaster]`, `[FLAC]`, `[320]`, `(Original Mix)`) as non-destructive tags.

## 5. Multi-Source Candidate Deduplication
- **Cross-Source Merging (Navidrome + Local Disk)**:
  - When scanning both remote API sources (Navidrome / Subsonic) and local filesystem paths, candidate tracks must be deduplicated via `deduplicate_candidate_tracks()` prior to reconciliation.
  - Fingerprint candidate tracks by `(norm_album, norm_title, track_number)`, shared MusicBrainz Recording/Track IDs, and path tails.
  - Merge ID tag sets and retain the physical local filesystem path. Prevent duplicate candidate instances of the same physical file from being left over to falsely satisfy other releases in fuzzy passes.

## 6. Network Requests & MusicBrainz Resolution
- **Search Query Caching**: Cache resolved artist queries in `artist_search_cache.json` and check existing `artist_*.json` files before making remote network calls.
- **Explicit Timeouts & IPv4/HTTP Fast Fallbacks**: Use `requests` with explicit timeouts when querying MusicBrainz endpoints to avoid indefinite `urllib` blocking.
- **Release Search Cache**: `search_release` results are cached for `SEARCH_CACHE_TTL` (7 days); failures are never cached, nor is the title-only fallback taken after a failed artist-restricted query. `force_refresh=True` bypasses the cache.
- **Aliases vs. Relations**: Only `is person` relations (`STRONG_ARTIST_RELATIONS`) are aliases. Other artist relations (bands, members, collaborators) go to `related_names` and count only when the artist's own credits name them; never treat them as the artist.

## 7. Soulseek & slskd P2P Discography Discovery Invariants
- **Non-Latin Unicode Script Preservation**:
  - `clean_search_phrase()` must **never** run `unidecode()` on non-Latin scripts (Japanese Kanji/Kana, Cyrillic, Chinese, Korean, Greek, etc.). P2P networks (Soulseek/slskd) preserve native Unicode filenames and tags.
  - Retain Unicode word characters (`re.UNICODE`) and strip only noise punctuation (Unicode P*/S*/Cf); keep combining marks so Indic and Thai words stay whole. An ASCII-folded Latin variant (`fold_search_phrase`) is an *additional* query, never a replacement.
- **Dual-Query Generation (User Query + Canonical Name)**:
  - Generate release queries for both the user's input query (`<UserArtist> <Release>`) and the canonical catalog name (`<CanonicalArtist> <Release>`), plus distinctive release titles.
  - Release queries and primary artist names must be prioritized at the head of the search queue. Do not allow obscure aliases to delay release searches.
  - Never send an artist MBID or MusicBrainz URL as search text (`is_artist_identifier`).
  - Track queries use `primary_artist(credit)` (featured guests and joint credits narrow results too much), then cleaned, accent-folded, bracket-less and `alt_titles` variants, then `<release> <title>` unless the title equals the release title.
- **Curated High-Yield Query Generation**:
  - Never flood slskd with dozens of granular track searches. Limit artist discovery to curated queries (canonical artist name, user input query, missing primary releases, and key compilations). The per-track fallback (`_search_unfilled_tracks`) runs only for songs no discovered folder or file supplied; at two searches at a time it can still add hundreds of searches for a large discography (also in `--dry-run`).
  - slskd 0.26 runs at most **two** searches at once; later ones wait `Queued` and burn their time limit there. `batch_search` keeps a sliding window of `MAX_CONCURRENT_SEARCHES = 2` and submits the next query as one finishes. Overlapping POSTs to `/searches` (and to `/transfers/downloads/{user}`) get HTTP 429: serialize submissions and retry 429.
- **slskd 0.26 Search Semantics** (`clients/slskd.py`, modelled by `tests/helpers/slskd026.py`):
  - **No responses until a search has finished.** Both `GET /api/v0/searches/{id}?includeResponses=true` and `GET /api/v0/searches/{id}/responses` return no responses while the search runs. `/responses` does **not** stream live responses; read it only as a fallback for a completed search with `responseCount != 0`. Never write tests that assume live responses.
  - slskd marks a record `Completed` shortly before it stores responses and `endedAt`. A completed record with `responseCount > 0` and an empty `responses` list is not final: poll again for up to `SEARCH_FINALIZE_WAIT` (10 s), then report an error rather than "no matches".
  - POST every search with explicit `searchTimeout` (`SEARCH_TIMEOUT_MS = 15000`, an **inactivity** timeout that restarts on every response), `responseLimit` (`DISCOGSEEK_SEARCH_RESPONSE_LIMIT`, default 500) and `fileLimit` (`DISCOGSEEK_SEARCH_FILE_LIMIT`, default 50000). Never rely on slskd's defaults (250 responses): broad artist/VA queries hit them within seconds.
  - Poll until the record is final. At the hard cap `max(timeout, SEARCH_MIN_WAIT=28) + SEARCH_GRACE_PERIOD=45` seconds (75 s for the CLI default of 30), counted from when slskd *started* the search (time spent `Queued`/`Requested` excluded, absolute ceiling 2 × cap after submission), send `PUT /api/v0/searches/{id}`: slskd stops the search and stores the responses so far (`Completed, Cancelled`). Never abandon a running search at a fixed deadline; its responses would be lost and it would keep holding one of the two slots.
  - A failed submission (429 is retried twice: 3 attempts), a 404, a search not finished `SEARCH_FINALIZE_WAIT` after the PUT, responses never stored, or a search ending `Errored`/`Cancelled` with **zero** responses (server disconnect, stopped while still queued) is a failure, not "searched, nothing found". `search()` raises `SlskdAPIError`; `batch_search` never raises per query and maps it to exactly `{"error": str, "responses": [], "searchText": q}`. Callers must detect `"error" in entry`, report a retryable search error, and never cache it.
- **Search Reuse, Attachment & Zombie Rows** (`use_existing`):
  - Reuse a completed history row only when it ended (or started) within `SEARCH_REUSE_MAX_AGE` (`DISCOGSEEK_SEARCH_REUSE_MINUTES`, default 60; `0` disables reuse), its state has no `Errored`/`Cancelled`, `fileCount > 0`, and reading it actually returns responses. Never reuse results of any age.
  - A row that completed normally with no files (`fileCount`, `responseCount` and `responses` empty; no `Errored`/`Cancelled`) is reused within `SEARCH_EMPTY_REUSE_MAX_AGE` (`DISCOGSEEK_EMPTY_SEARCH_REUSE_MINUTES`, default 15, capped by `SEARCH_REUSE_MAX_AGE`; `0` disables it), and only when no search of the query is running and no reusable row of it has files: a readable row with files always wins, and an unreadable one means search again.
  - Attach to a running row only when it started within the hard cap, and count its time limit from slskd's start time. Older `InProgress` rows are zombies left by a slskd restart: they never finish, cannot be stopped, and return nothing. Ignore them and search afresh; DELETE them (best effort) only once older than `max(SEARCH_ABANDONED_AGE=300 s, 3 × hard cap)`, since another client's long search may still be legitimate.
  - `batch_search` deletes completed zero-file rows of a query only when nothing for it is reusable or running.
  - `force_refresh` / `refresh_searches` pass `use_existing=False`. Within one `download_missing_tracks` call a query→result cache is shared by the album and track phases (zero-result searches are cached, failed ones are not); `search_cache` lets several calls share it.
  - Parse slskd timestamps with up to 7 fractional digits, `Z`, a `±hh[:]mm` offset, or no zone (UTC).
- **Strict Release Directory Matching (`is_dir_name_match`)**:
  - Compilations, EPs, and single-track features must verify that the remote peer directory name matches the release title or catalog code.
  - Prevent unrelated album directories with single bonus remix tracks from being falsely assigned as the release folder.
- **Generic Dump Folder Detection (`is_loose_dump`)**:
  - Detect generic unsorted folders (`dump`, `archive`, `tracks`, `music`, `songs`, `shared`, `root`).
  - Only enqueue the individual matching track file from dump directories, never the entire folder.
- **Safe Enqueueing & Peer Username Encoding**:
  - Always clean and URL-encode peer usernames (`urllib.parse.quote(clean_user, safe="")`) to support special symbols, brackets, and IP suffixes.
  - Chunk download enqueue requests into batches of $\le 50$ files with $\ge 30$s timeout to prevent HTTP timeout errors.
  - Ensure all standard library modules (`re`, `urllib.parse`) are explicitly imported at the top of client modules.
  - HTTP 429 on enqueue is retried after `ENQUEUE_BUSY_DELAYS` (1, 2, 4 s). A 200/201 body `{enqueued, failed}` can report partial failures: slskd 0.26 serializes its `(Filename, Message)` value tuples as `{}`, so a failure is tied to a file only when its filename is one of the requested files. Entries saying "skipped"/"already in progress/queued" are accepted; unidentifiable failures are resolved against live transfers (`get_downloads`). Rejected files raise `SlskdEnqueueError` carrying `queued_files` (earlier chunks plus the accepted subset); later chunks are not submitted. Only files slskd definitely did not queue may be re-sourced: after a lost reply or server error a file may already be queued.
  - "Failed to retrieve endpoint for user" (5xx) means the peer is unreachable (`SlskdPeerUnavailableError` after retries at 1 and 2 s).
- **slskd 0.26 Browse Shape**: `browse_directory` receives `[{name, fileCount, files: [{filename: basename}]}]` (or one dict) and must flatten it to files whose `filename` is the full remote path (`name + "\\" + basename`), keeping only the requested directory when the reply contains it (case- and separator-insensitive). Browsing drops subdirectories: request implied disc siblings (`CD1` → `CD2..N`) explicitly, and browse each folder at most once per run.

## 8. Strict Release, Container & Audio Format Deduplication Invariants
- **Catalog Release Group & Normalized Title Deduplication**:
  - MusicBrainz catalogs must deduplicate release entries by `(is_va, normalize_text(title), release_group_id)`. Multiple editions (e.g. CD vs Bandcamp Web vs Remasters) must not produce duplicate release objects or evaluation loops.
  - Primary releases (`is_va=False`) and compilation releases (`is_va=True`) must be strictly segregated. Primary reconcilers must only evaluate primary releases; compilation reconcilers must evaluate compilations once without re-evaluating primary albums.
  - A release's family is every edition with the same title **or the same release group** (`_release_family`): an expanded or deluxe edition's bonus tracks belong to the album even when the catalog lists only one edition. Positions come from the edition found in the library, otherwise from the fullest edition. In `ArtistCatalog`, an edition titled like its release group replaces a promo or differently titled edition seen first.
- **Cross-Stage Track Coverage & Spillover Prevention**:
  - Coverage, "already queued" checks and history protection use **song keys** (`_song_keys`: recording IDs plus `(release group, title, occurrence)` slots), not normalized titles. When a folder is accepted, mark the songs it supplies as covered (`covered_song_keys`); `covered_track_titles` only additionally keeps standalone passes from re-searching covered titles.
  - Standalone reconcilers must never search for leftover track variations of already matched/queued releases to prevent secondary single downloads in alternative formats.
  - The appearance/DJ-mix fallback (`download_scope` `artist_tracks`) fetches each of the artist's recordings once: recordings already local or queued for another release are skipped.
- **Release & Track-Level Queue Deduplication**:
  - Every accepted folder can contribute: all accepted folders of a release feed one queue walked in score order (preferred format first), a folder must supply at least one still-missing song, and each song is queued once. A folder is accepted on its match ratio over the edition union or any single edition. Disc subfolders (`CD1`/`CD2`, `Album (Disc 2)`) are evaluated as one copy.
  - **Queue Deduplication by Copy, not Basename**: never dedupe on a global lowercase basename (`01 - intro.flac` exists in thousands of albums). Dedupe by exact remote path, or the same `(release folder, basename)` copy from another peer. History copies in folders named after another release (including releases missing from the catalog) never protect a song; year, format and placeholder folders (`2019`, `MP3 320`, `Unknown Album`) name no release.
  - **Selective Partial-Album Queueing**: When a release is partially present locally (e.g., 10 of 11 tracks present), download engines must filter candidate directory files to enqueue ONLY the genuinely missing tracks, preventing all-or-nothing full album re-downloads.
  - **Local Library Pre-Seed Coverage Invariant**: Releases identified during local library pre-scan must immediately pre-seed `reconciled_release_keys` and `covered_track_titles` to prevent downstream compilation and standalone passes from re-evaluating and downloading tracks already satisfied locally.
- **Fuzzy Platform Title Skipping**:
  - Bandcamp, MediaFire, Archive.org, and web scrapers must use fuzzy normalized title matching (stripping `[EP]`, `[LP]`, `Single`, `Remaster`, `VIP`, etc.) against server and queued releases, skipping downloads when $\ge 80\%$ of artist tracks are already present or queued.
- **Intra-Directory Audio Container Conflict Safety**:
  - Stream fallback downloaders (e.g. Bandcamp MP3-128 stream fallback) must check if ANY supported audio file (`.flac`, `.wav`, `.m4a`, `.mp3`, etc.) already exists for that track index/title before writing fallback MP3 stream files.

## 9. P2P Candidate Indexing & High-Performance Verification Invariants
- **Inverted Candidate Indexing (`PeerCandidateIndex`)**:
  - Never evaluate releases or tracks via nested $O(N \times M)$ linear scans across thousands of candidate peer directories and files.
  - Construct an inverted dictionary index (`word_to_dirs` mapping tokens $\ge 3$ characters to candidate directory objects, and `word_to_files` mapping tokens to candidate file objects) immediately following discovery.
  - Release and track verification must query the inverted index to prune candidate search spaces from thousands down to $\le 20$ candidates in $<1\text{ms}$.
- **Two-Phase Release Evaluation & Batch Directory Browsing**:
  - **Phase 1**: Perform instant in-memory evaluation on candidate directories populated with search result files.
  - **Phase 2**: If no candidate directory reaches `min_match_ratio`, concurrently batch-browse the top matching candidate directories via `browse_directories_batch()` and re-evaluate.
  - Never invoke synchronous, sequential `browse_directory` calls inside loops across dozens of remote peers.
- **Pre-Parsed Track & Directory Data Structures**:
  - Pre-parse audio format scores, filename normalization, tokens, and structural title parts (base title, version modifiers, remaster descriptors) **once** during index creation (`CandidateFile` / `CandidateDir`).
  - Pre-parse expected catalog tracklists once per release (`pre_parse_expected_tracks()`) rather than re-normalizing per comparison.
- **LRU Caching & Pre-Compiled Regexes for Normalization**:
  - Decorate frequently called string normalization and distance functions (`normalize_text`, `strip_track_number_and_artist`, `parse_track_title_structure`, `calculate_similarity`) with `@lru_cache(maxsize=65536)`.
  - Ensure all arguments to `@lru_cache` functions are strictly hashable (`str`, `tuple`, `frozenset`). Never pass mutable `set` or `dict` objects.
  - Pre-compile all static regular expressions (`re.compile`) at module level.
  - Fast-path identical strings (`if str1 == str2: return 1.0`) to avoid matrix distance calculations.
- **Index Synchronization on Remote Browse**:
  - When fetching complete folder listings from peers via API (`browse_directory`), update the active inverted index via `candidate_index.update_directory(user, dir_name, dir_info)` so that subsequent release and track reconciliation passes immediately benefit from the complete folder file list. `update_directory` must refresh every index (all dirs, word and base maps, disc groups); a browsed disc folder re-evaluates its merged parent.
- **Artist Evidence Is Word-Bounded**: An alias counts only as whole words of a folder name or credit; a one-word alias inside a longer name is not the artist ('Lone' is not 'Lone Wolf', 'K-Lone' or 'Two Lone Swordsmen'). Use `artist_credit_matches` for credits.

## 10. Multi-Lingual & Numeric Title Normalization
- **Kanji Numeral Parsing**: Catalog track titles with Japanese Kanji numbers (e.g. `九十四`, `一`, `十`) must be converted to Arabic digits (`94`) during normalization to ensure alignment with standard numeric file tags.
- **Purely Numeric Track Title Safety**: Track-number stripping routines (`strip_track_number_and_artist`) must never strip purely numeric titles (e.g., `"94"`, `"1999"`, `"007"`) down to empty strings. If stripping leaves an empty string, fall back to the original non-extension filename.

## 11. Compilation & Various Artists Release Invariants
- **Album Artist & Compilation Flag Extraction**:
  - `AudioQualityAnalyzer` must extract `album_artist` from exact frames (`TPE2`, `ALBUMARTIST`, `aART`) and compilation flags (`TCMP`, `cpil`, `COMPILATION`).
  - Standardize all compilation aliases (`va`, `v.a.`, `various`, `various artists`, `compilation`) to `"Various Artists"`.
  - Normalize typo variations on peer folders and tags (e.g. `"Various Artitsts"`, `"Various Artist"`, `"V. Artists"`) via regex `^v(?:arious|\.)?\s*arti[st]{2,4}s?$`.
- **Contributing Track Independence**:
  - Never allow a single contributing artist track to define the release-level artist on a compilation or multi-artist directory.
  - In compilation releases, individual tracks must always retain their true track-level artist credits (`artist-credit` from tags or MusicBrainz).
- **Various Artists Only for VA Credits**:
  - A release is Various Artists only when MusicBrainz credits Various Artists (`is_various_artists`, which applies NFKC and accepts `V.A`, `v. a.`, `v a` and fullwidth spellings), or when it is a Compilation-type release whose tracks are mostly by artists other than its credited curator/label/DJ. Single-artist best-ofs and remix albums keep their artist; `is_compilation` is reported separately.
  - Local compilation detection counts artists per `(folder, album)` using primary artists, so featured guests (`Lone feat. Guest`) never make an album VA and two albums flattened into one slskd `CD2` folder stay separate. An album without an album-artist tag is named after its dominant **main** credit without featured guests (`Simon & Garfunkel`, not `Simon`).
  - Download compilation mode (`library_download`) comes only from the release artist, never from a featured guest on some tracks.
- **Soulseek Querying for Compilations**:
  - Start compilation album searches with the cleaned bare title, then the raw title. Do not prefix `"VA"` or `"Various Artists"`: their results are a subset of the bare title's.
  - When the title has a single significant word or its search reaches a search limit (`ResponseLimitReached`/`FileLimitReached`), add up to `MAX_QUALIFIED_QUERIES` (4) `"<title> <credited artist>"` queries from missing and present tracks' primary artists, and after each pair read up to `MAX_FOLDER_BROWSES` (2) peer folders that showed only part of the release. `"Various Artists <title>"` runs last, and only after a truncated search.
  - Missing compilation tracks are searched by their own primary artist. Without a known track artist, use `"Various Artists <Track Title>"` variants and `"<Release Title> <Track Title>"`. Never prepend a single contributing artist's name to all missing tracks on a compilation.

## 12. Unified Multi-Folder & Partial-Download Release Grouping
- **Natural Release Key Invariant**:
  - Library scanning must group releases by natural key (`va_{norm_album}` for compilations, `art_alb_{norm_artist}::{norm_album}` for single artists), rather than splitting by directory hash or MBID.
  - Untagged downloads (from Soulseek or Bandcamp) and MBID-tagged local files for the same album must be unified into a single release.
  - If tracks on the album carry a `musicbrainz_albumid` tag, propagate the MBID most of its files carry (ties: the smallest ID) to `rel["mb_release_id"]` so that MusicBrainz reconciliation immediately utilizes it. A mixed-edition folder must not take whichever MBID was scanned first.
  - Grouping must not depend on directory order: the scan walks directories and files sorted (network filesystems list entries in server order).
- **Folder Precedence & Deduplication**:
  - When audio tracks for an album exist across multiple folders (e.g. `Library/` and `downloads/`), the release's primary `folder_path` must prioritize canonical non-download library paths.
  - Deduplicate multiple file instances of the same track across folders, prioritizing files in `Library/` over `downloads/`.

## 13. Incremental Re-Audit & Client State Synchronization
- **Fast Incremental Rescan on Re-Audit**:
  - Triggering a release re-audit (`force_refresh=True`) must trigger a fast incremental library rescan (`force_disk_rescan=False`) before MusicBrainz reconciliation. This reuses cached SQLite metadata for existing files ($<1\text{s}$) while instantly picking up newly downloaded files on disk.
  - Never reuse stale in-memory cached release objects when the user explicitly requests a re-audit.
- **Frontend ID Resilience & Selection Sync**:
  - When a library rescan or audit completes, the frontend must re-bind `AppState.selectedReleaseId` and `AppState.selectedReleaseData` using fallback matching on `(title, artist)` in case release IDs unify or shift.
  - Re-audits must update both the master release list item and the active details pane without requiring a full page refresh.

## 14. Missing Track Downloader & Placeholder Resolution Invariants
- **Synthetic Gap Placeholder Resolution**:
  - Local library scanning generates synthetic placeholders (`Track XX (Missing)`) for missing sequence numbers. Download engines (`download_single_missing_track`, `download_missing_tracks`) must **never** query P2P networks with synthetic placeholder strings.
  - When a download request contains a placeholder track or track number, automatically invoke `audit_release` to resolve the official title and track-level artist from MusicBrainz before dispatching search queries.
  - If a placeholder track cannot be resolved via MusicBrainz, abort with a clear, actionable error rather than wasting network timeout budgets searching for generic strings like `"Track 01 (Missing)"`.
  - `is_unknown_album` also treats `[standalone recordings]`, `[non-album tracks]` and `unknown` as placeholder album titles: never search for them as releases.

## 15. Release Verification (`services/library_audit.py`)
- **Printed Titles + `alt_titles`**: An official track's `title` is the printed track title (MusicBrainz track title, falling back to the recording title); `alt_titles` holds the recording title when it differs. Local files, peer files and history are matched against both, parsed with `is_filename=False`. Official rows also carry `position_token` (`B2`), `track_position` (index within the medium), `absolute_position` (index across all media) and `medium_track_count`; downstream matchers must use them instead of re-deriving positions.
- **MBID Pass First**: Pass 1 matches every official track by recording or track MBID before any title pass. A recording-only match is skipped when the file's track ID names another track of the same edition, and title passes skip files whose MBIDs identify another track of the edition.
- **Position Rules**: A local file matches when any position form agrees: its medium position, its absolute position (flat numbering without disc tags, or continuous numbering that falls inside the file's own tagged disc, e.g. `19` as disc 2 track 1; any other out-of-range number is title-only evidence), or its vinyl side via `vinyl_position`. Local side letters map to MusicBrainz media and are never disc numbers. When local discs are unknown or flat, a title unique across the whole tracklist may match on another disc. A numbered file at a different position matches only when its full title, version included, is identical.
- **Sibling-Edition Recording Coverage** (Pass 4): A missing recording is covered by an exact recording-MBID match from a sibling edition at **any** position when the recording appears only once on this edition; a repeated recording needs its own position. Siblings share the natural key, the compact key (spaces and punctuation removed: `Machine Drum` = `Machinedrum`), or a MusicBrainz release group learned from audits. Covered rows carry `covered_by_edition`. When a scan learns a release group that links an incomplete edition to a sibling holding its missing recordings, that edition is audited again.
- **Extras and Unverified Results**: Unmatched local files stay as found rows with `is_extra=True` and never count toward `found_count`, `total_tracks_expected` or `completion_pct`. A release that matches none of the local files is unverified (`NO_MATCH_ERROR`). A title-only MusicBrainz search accepts only candidates crediting the local artist (`artist_credit_matches`, spelling variants allowed), fetches at most 3, and keeps the one covering the most local tracks; otherwise the release is unverified. Sequence gaps alone never establish completeness.
- **Unrelated Same-Title Releases**: `_resolve_local_aliases` skips untagged same-title releases with no shared artist credit and no shared recording instead of raising; it still raises for a plausible copy that cannot be verified.

## 16. Transfer History, Stale Queues & Peer Policy (`core/transfers.py`, `core/peer_policy.py`)
- **Terminal Failures**: A transfer is a terminal failure when its state contains `Completed` and one of `Cancelled`, `Aborted`, `Rejected`, `TimedOut`, `Errored` (`terminal_failure`). Never count one as queued or successful.
- **slskd Retry Budget**: `Rejected`, `Cancelled` and `Aborted` are final at once. `TimedOut`/`Errored` are final (`is_final_failure`) only when `attempts >= transfers.download.retry.attempts`, or when no retry is pending (`nextAttemptAt` empty, unset .NET year 1, unparseable, or over 15 minutes in the past). slskd 0.26 sends no `nextAttemptAt` and keeps one record across retries, so the browser waits out `retry.maxDelay` plus a margin with the record unchanged before switching sources. `DownloadHistory` given the budget (`retry_attempts`/`retry_max_delay`, or the `slskd_retry_budget` context the browser sets around its requests) treats such a record as protected while `attempts` is below the budget and a retry is pending or `endedAt` is within `retry.maxDelay` + `RETRY_DELAY_MARGIN_SECONDS`, unless discogseek already cancelled it (internal-cancel marker). An unknown budget (options unreadable, or the CLI downloader) means final. `prepare_recovery` never cancels a completed failure that still has discogseek's internal-cancel marker.
- **Stale Queue Policy**: A transfer whose states include `Queued`, `Requested` or `Initializing` but none of `InProgress`, `Completed`, `Succeeded`, and whose latest `requestedAt`/`enqueuedAt`/`startedAt`/`endedAt` is older than `STALE_QUEUE_SECONDS` (`DISCOGSEEK_STALE_QUEUE_HOURS`, default 6), is stale (`is_stale_queued`). Ignore `bytesTransferred`: retries keep partial bytes from earlier attempts. A stale transfer no longer protects its track and its source is excluded, but it is cancelled (as an internal cancel) **only** when a replacement for that row is queued; without a replacement it stays queued. The browser does not flag transfers of a peer that is sending a file or finished one within the stale window.
- **Succeeded Protection Only While the File Awaits Import**: A `Succeeded` transfer protects its track only while its local copy still sits in slskd's downloads folder (`<downloads>/<remote parent folder>/<basename>`, including slskd's `<stem>_<digits><ext>` rename and `_`-substituted unsafe characters). `resolve_downloads_dir` uses `DISCOGSEEK_SLSKD_DOWNLOADS_DIR` when set, else slskd's `directories.downloads` option when that path exists locally. Without a readable folder, protect for `IMPORT_WINDOW_SECONDS` (`DISCOGSEEK_IMPORT_WINDOW_HOURS`, default 24) after `endedAt` (or when `endedAt` is missing). After that the transfer is `completed`: neither protected nor excluded, and it clears an older failure of the same source. In release downloads a `Succeeded` transfer satisfies a row only when it came from that release's own folder, or when the submission log (`core/submissions.py`, `submissions.db` in the cache dir) records that discogseek submitted it for that very row (never for a radio/mix folder that does not name the release). The browser demotes `DOWNLOADED` tracks to missing 10 minutes after finishing once the file has left the folder (or after the import window without folder access).
- **Precedence**: protected beats stale and failed; stale beats failed for the same source; a success clears a failure only when the failure is not newer by `endedAt`.
- **Reason-Based Peer Policy** (`classify_failure` on the state and slskd's exception text):
  - `PEER_BLOCK`, until `discogseek peers unblock`: `Rejected` with banned / verification required / blocked country, and a manual `Cancelled` (not discogseek's own, not part of a bulk cancellation).
  - `PEER_COOLDOWN` for `PEER_COOLDOWN_SECONDS` (`DISCOGSEEK_PEER_COOLDOWN_HOURS`, default 6) counted from the refusal's `endedAt`: every other `Rejected` (`Too many files`, `Overwhelmed`, internal error, unexplained). After the cooldown the refused file is offered again.
  - `FILE_ONLY`: `Rejected` for not shared / file read error / remote file error / disallowed extension / size mismatch, and `Aborted` (the remote size changed). Exclude only that file.
  - `None`: `TimedOut`, `Errored`. Exclude the failed file; no judgment of the peer.
  - Bulk cancellations: at least `BULK_CANCEL_MIN` (3) cancellations ending within `BULK_CANCEL_WINDOW` (1 s) of each other that discogseek did not make are system events (slskd shutdown, bulk cleanup). They neither block nor exclude, are not recovered automatically, and a `Cancelled` block whose event turns out to be part of such a cluster is lifted.
  - Call `record_internal_cancel` **before** `cancel_download` (and `forget_internal_cancel` if it fails) so recovery cleanup is never read as a manual cancellation.
  - A cooldown never shortens or replaces a lasting block, and a `Cancelled` block never replaces a refusal block such as Banned. Stored blocks are re-classified on every open of `peer_policy.db`: lasting blocks stay, transient ones become a cooldown from `blocked_at` or are deleted when its start is unknown or expired, file-level ones are deleted.
  - `blocked_peers()` (what `discogseek peers` lists) returns lasting blocks and unexpired cooldowns; `peer_blocks()` adds `blocked_at`/`expires_at`.
- **Recovery Scope**: `DownloadHistory.prepare_recovery(client, sources)` must always be called with the specific sources of the row being replaced, never bare; cancelling every encountered failure would stop unrelated stale transfers. A missing transfer ID is an error, never a silent skip.

## 17. Text, Artist Credits & Positions (`core/text.py`, `core/release_metadata.py`)
- **Normalization**: `normalize_text` turns `_` into a space and `&` into `and`, applies the Polish diacritics table, and for symbol-only text falls back to casefolded symbols (`★`) instead of an empty key. Changing it changes library-scan release IDs (md5 of the normalized key) and the browser audit cache keys.
- **Title Parsing Modes**: `parse_track_title_structure(title, is_filename=True)` strips one real audio extension, the track token and the artist prefix, splits scene names (`NN-artist-title(-group)`, `Artist_-_Title`) and keeps numbers that belong to the title (`2 Is 8`, `1999`, `007`). With `is_filename=False` (MusicBrainz and tag titles) never strip numbers, artist prefixes or extensions and never split at ` - `. Feature credits are removed before version detection; `(<name> mix)` is the remix family; `(v2)` / ` - v2` are second-version markers, not noise. Compare peer filenames with `title_match_score` over `title_variants` (≥ 0.85 release scope, ≥ 0.80 track scope).
- **Artist-Credit Splitting**: `split_artist_credit` splits on feat./ft./featuring/with/and/x/vs. (a name on both sides) and on `, ; & + /`, primary first; band names are split too (`Simon & Garfunkel` → `simon`, `garfunkel`). Use `primary_artist` for search queries only; never use it to name an album (keep the main credit). `artist_credit_matches` matches equal names, token-subset containment where the smaller name has at least 2 words, or similarity ≥ 0.90. One shared word never matches (`Lone` vs `Lone Wolf`, `Iron & Wine` vs `Iron Maiden`), and an empty remote never matches. Callers handle Various Artists.
- **Position Rules** (`parse_disc_and_track_number`, `vinyl_position`, `disc_number_from_folder`):
  - A filename vinyl prefix applies only to sides A–H with 1–2 digits (≤ 30) and a separator, so `M83 - `, `U2 - `, `C418`, `E40` and `B52s` carry no position.
  - A filename side/disc/vinyl prefix whose number contradicts a numeric track tag is ignored. `0DTT` → (DD, TT); `00NN` → track NN; a year is no position. A 3-digit `DNN` → (D, NN) only with a known `disc_count` and 1 ≤ D ≤ `disc_count`; the downloader reads `205` as disc 2 track 5 only when no medium has 100 or more tracks.
  - `disc_number_from_folder` returns 1..20 only. It accepts whole-name disc folders (`CD 2`, `Disc One`), disc brackets with subtitles (`Album (Disc 2)`, `[CD 4 of 5]`), trailing suffixes (`Album - CD 2`) and disc tokens outside brackets; it rejects catalog numbers inside brackets (`[WARP CD 6]`), years (`CD-2002`), ranges (`cd 1 & 2`) and `Side B`. Callers also ignore a value above the release's medium count.
  - In a release download, positions are enforced only in a folder that names the release or holds two other tracks of it; a numbering conflict there is waived only for an exact, unrepeated title whose slskd length is within 3 s of the MusicBrainz duration, and never for complete-release verification. Outside the release folder the title must be exact (or the position the same), and a clearly different length rules the file out. A repeated title (`Intro`, `Interlude`) is taken only from this release's folder.






