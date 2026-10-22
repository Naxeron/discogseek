"""MusicBrainz release lookup and reconciliation against local library tracks."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from discogseek.clients.musicbrainz import MusicBrainzClient
from discogseek.core.release_metadata import (
    format_artist_credit,
    is_unknown_album,
    is_various_artists,
    parse_disc_and_track_number,
    vinyl_position,
)
from discogseek.core.text import (
    are_versions_compatible,
    artist_credit_matches,
    calculate_similarity,
    normalize_text,
    parse_track_title_structure,
    split_artist_credit,
)
from discogseek.services.reconciler import (
    have_conflicting_numbers,
    is_purely_numeric_track,
)


# Search results are only candidates: an untagged album is verified against at most
# this many same-title MusicBrainz releases, each of which must credit its artist.
_MAX_SEARCH_CANDIDATES = 3
_FILENAME_VINYL_RE = re.compile(r"^([A-Ha-h])(\d{1,2})(?:[\s._\-]|$)")
_BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# Words that make a bracketed part a version of the recording rather than a subtitle.
_VERSION_SUBTITLE_RE = re.compile(
    r"\b(?:version|ver|mix|remix|rmx|edit|dub|vip|rework|bootleg|flip|refix|cut|take)\b"
)
NO_MATCH_ERROR = "MusicBrainz release matches none of the local tracks"


def is_numeric_track_item(lt: Dict[str, Any]) -> bool:
    """Checks if track is purely numeric or compound disc-track number with no title."""
    if is_purely_numeric_track(lt):
        return True
    raw_title = (lt.get("title") or "").strip()
    raw_fn = Path(lt.get("filename") or lt.get("path") or "").stem.strip()
    return bool(
        re.match(r"^\d{1,2}[-_.]\d{1,3}$", raw_fn)
        and (
            not raw_title
            or raw_title.isdigit()
            or raw_title == raw_fn
            or re.match(r"^(?:track|trk)[\s._\-]*\d{1,3}$", raw_title, re.IGNORECASE)
            or re.match(r"^\d{1,2}[-_.]\d{1,3}$", raw_title)
        )
    )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _local_number(lt: Dict[str, Any]) -> Optional[int]:
    if lt.get("track_num_int") is not None:
        return _int_or_none(lt.get("track_num_int"))
    raw = str(lt.get("track_number") or "").strip()
    digits = re.sub(r"[^\d]", "", raw.split("/")[0].split("-")[-1])
    return _int_or_none(digits) if digits else None


def _local_vinyl(lt: Dict[str, Any], disc: int, number: Optional[int]) -> Optional[Tuple[str, int]]:
    """The vinyl side position a local track's own tag or filename names ('B2')."""
    for token in (lt.get("position_token"), lt.get("track_number")):
        side = vinyl_position(token) if token else None
        if side:
            return side
    match = _FILENAME_VINYL_RE.match(Path(lt.get("filename") or lt.get("path") or "").name)
    if match and number is not None:
        side = (match.group(1).upper(), int(match.group(2)))
        # The scan maps 'B2' to disc 2 track 2; a prefix the tags overruled names nothing.
        if side[1] == number and ord(side[0]) - ord("A") + 1 == disc:
            return side
    return None


def _official_tracklist(mb_release: Dict[str, Any], rel_artist_hint: str) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]]
]:
    """Official tracks with printed titles and every position form; and the media summary."""
    media_list = mb_release.get("medium-list", [])
    # Flat numbering counts tracks across media in release (medium position) order.
    order = sorted(range(len(media_list)), key=lambda index: (
        _int_or_none(media_list[index].get("position")) or index + 1, index,
    ))
    absolute_start: Dict[int, int] = {}
    running = 0
    for index in order:
        absolute_start[index] = running
        running += len(media_list[index].get("track-list", []))

    official_media: List[Dict[str, Any]] = []
    official_tracks: List[Dict[str, Any]] = []
    for medium_index, medium in enumerate(media_list):
        # MusicBrainz positions are strings. Keep official and unmatched
        # local tracks numeric so their combined list sorts consistently.
        disc_num = int(medium.get("position") or 1)
        track_list = medium.get("track-list", [])
        official_media.append({
            "position": disc_num,
            "format": medium.get("format"),
            "track_count": len(track_list),
        })
        for track_index, trk in enumerate(track_list, 1):
            rec = trk.get("recording", {})
            trk_num = trk.get("number") or str(trk.get("position", ""))
            # musicbrainzngs sets a track title only where the printed title differs
            # from the recording; local files and peers follow the printed title.
            printed = (trk.get("title") or rec.get("title") or "Unknown Track").strip() or "Unknown Track"
            recording_title = (rec.get("title") or "").strip()
            alt_titles = [recording_title] if (
                recording_title and recording_title.casefold() != printed.casefold()
            ) else []
            trk_len = trk.get("length") or rec.get("length")
            duration_sec = round(int(trk_len) / 1000.0, 1) if trk_len else None
            own_credit = format_artist_credit(trk.get("artist-credit") or rec.get("artist-credit"))
            official_tracks.append({
                "disc_number": disc_num,
                "track_number": trk_num,
                "title": printed,
                "alt_titles": alt_titles,
                "position_token": str(trk_num or ""),
                "track_position": _int_or_none(trk.get("position")) or track_index,
                "absolute_position": absolute_start[medium_index] + track_index,
                "medium_track_count": len(track_list),
                "own_artist": own_credit,
                "artist": own_credit or rel_artist_hint,
                "norm_title": normalize_text(printed),
                "mb_recording_id": rec.get("id"),
                "mb_track_id": trk.get("id"),
                "duration": duration_sec,
            })
    return official_tracks, official_media


def _official_keys(off: Dict[str, Any]) -> Set[Tuple[Any, ...]]:
    disc = int(off["disc_number"] or 1)
    keys: Set[Tuple[Any, ...]] = {("medium", disc, off["track_position"]), ("absolute", off["absolute_position"])}
    token = str(off.get("position_token") or "").strip()
    number = _int_or_none(token) if token.isdigit() else None
    if number is not None:
        # Some releases number tracks continuously across media ('12' on disc 2).
        keys.add(("medium", disc, number))
    side = vinyl_position(token)
    if side:
        keys.add(("side",) + side)
    return keys


def _artist_names(mb_release: Dict[str, Any], official_tracks: List[Dict[str, Any]], mb_artist: str) -> List[str]:
    """Release and track credits, including each credited artist's MusicBrainz name."""
    names = [mb_artist] + [track["own_artist"] for track in official_tracks]
    credits = list(mb_release.get("artist-credit") or [])
    for medium in mb_release.get("medium-list", []):
        for trk in medium.get("track-list", []):
            credits.extend(trk.get("artist-credit") or trk.get("recording", {}).get("artist-credit") or [])
    for credit in credits:
        if isinstance(credit, dict):
            names.append(credit.get("name") or "")
            names.append((credit.get("artist") or {}).get("name") or "")
    unique: List[str] = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    return unique


def _local_credits(release_data: Dict[str, Any], local_tracks: List[Dict[str, Any]]) -> List[str]:
    names = [release_data.get("album_artist"), release_data.get("artist")]
    for lt in local_tracks:
        names.append(lt.get("artist"))
        names.extend(lt.get("artists") or [])
    credits: List[str] = []
    for name in names:
        name = (name or "").strip()
        if (name and name not in credits and not is_various_artists(name)
                and normalize_text(name) not in ("unknown artist", "unknown")):
            credits.append(name)
    return credits


def _has_artist_evidence(
    mb_release: Dict[str, Any], official_tracks: List[Dict[str, Any]], mb_artist: str,
    release_data: Dict[str, Any], local_tracks: List[Dict[str, Any]],
) -> bool:
    """
    Whether a MusicBrainz release found by title search credits the local album's artist.
    A same-title album by another artist ('Machinedrum — Now You Know' vs 'Madame
    Mayhem — Now You Know') is not this release. Spelling variants such as
    'Machinedrum' and 'Machine Drum' match.
    """
    local = _local_credits(release_data, local_tracks)
    if not local:
        # An album without any artist credit can only be checked by its tracks.
        return True
    official = [name for name in _artist_names(mb_release, official_tracks, mb_artist)
                if not is_various_artists(name)]
    if not official:
        # A compilation without per-track credits cannot confirm its contributors.
        return is_various_artists(mb_artist) or not mb_artist
    return any(artist_credit_matches(expected, name) for expected in official for name in local)


def _is_multi_artist_compilation(mb_artist: str, official_tracks: List[Dict[str, Any]]) -> bool:
    """A compilation credited to a label, DJ or curator rather than to its tracks' artist."""
    if not official_tracks:
        return False
    names = split_artist_credit(mb_artist)
    pattern = re.compile(rf"(?<![^\W_]){re.escape(names[0])}(?![^\W_])") if names else None
    own = 0
    for track in official_tracks:
        credit = track["own_artist"]
        if (not credit or artist_credit_matches(mb_artist, credit)
                or (pattern is not None and pattern.search(track["norm_title"]))):
            own += 1
    return own * 2 < len(official_tracks)


def _parsed_titles(title: str, alt_titles: List[str]) -> List[Dict[str, Any]]:
    parsed = []
    for variant in [title] + list(alt_titles or []):
        if variant:
            structure = parse_track_title_structure(variant, is_filename=False)
            if structure["base_norm"]:
                parsed.append(structure)
    return parsed


def _full_norm(parsed: Dict[str, Any]) -> str:
    return normalize_text(f"{parsed['base_title']} {parsed['version_text'] or ''}").replace(" ", "")


def _bare_norm(parsed: Dict[str, Any]) -> str:
    """The base title without bracketed subtitles: 'Ninja Tune (Enter the Augmenter)'."""
    return normalize_text(_BRACKETED_RE.sub(" ", parsed["base_title"] or ""))


def _omits_only_subtitles(p_off: Dict[str, Any], p_lt: Dict[str, Any]) -> bool:
    """
    Whether two titles differ only by bracketed subtitles. Brackets the version parser
    leaves in the base title can still name another version ('Welt Am Draht (Die Vögel
    version)', 'Skylla (Bleep version)'); a title without one is not that version.
    """
    if not _bare_norm(p_off) or _bare_norm(p_off) != _bare_norm(p_lt):
        return False
    brackets = [
        {normalize_text(match.group(0)) for match in _BRACKETED_RE.finditer(parsed["base_title"] or "")}
        for parsed in (p_off, p_lt)
    ]
    return not any(_VERSION_SUBTITLE_RE.search(text) for text in brackets[0] ^ brackets[1])


def _title_score(
    official_titles: List[Dict[str, Any]], p_lt: Dict[str, Any], at_position: bool = False,
) -> Optional[Tuple[bool, float, float]]:
    """
    (full titles equal, base similarity, version similarity) of the best compatible
    title. At the file's own position, a title that only omits a bracketed subtitle
    of the official one ('Ninja Tune (The Process mix)') counts as similar.
    """
    best = None
    if not p_lt["base_norm"]:
        return None
    for p_off in official_titles:
        if have_conflicting_numbers(p_off["base_norm"], p_lt["base_norm"]):
            continue
        if not are_versions_compatible(
            p_off["version_type"], p_off["version_text"], p_lt["version_type"], p_lt["version_text"],
        ):
            continue
        similarity = 1.0 if p_off["base_norm"] == p_lt["base_norm"] else calculate_similarity(
            p_off["base_norm"], p_lt["base_norm"],
        )
        if at_position and similarity < 0.85 and _omits_only_subtitles(p_off, p_lt):
            similarity = 0.85
        v_off, v_lt = p_off["version_text"] or "", p_lt["version_text"] or ""
        version_similarity = 1.0 if v_off == v_lt else calculate_similarity(v_off, v_lt)
        score = (_full_norm(p_off) == _full_norm(p_lt), similarity, version_similarity)
        if best is None or score > best:
            best = score
    return best


def _reconcile(
    mb_release: Dict[str, Any], release_data: Dict[str, Any], local_tracks: List[Dict[str, Any]],
    artist: str,
) -> Dict[str, Any]:
    """Match local tracks and sibling-edition recordings to the official tracklist."""
    mb_artist = format_artist_credit(mb_release.get("artist-credit")) or mb_release.get(
        "artist-credit-phrase", ""
    )
    rg = mb_release.get("release-group", {})
    rg_type = (rg.get("primary-type") or "").lower()
    rg_sec_types = [t.lower() for t in rg.get("secondary-type-list", [])]
    is_compilation = rg_type == "compilation" or "compilation" in rg_sec_types
    official_tracks, official_media = _official_tracklist(mb_release, mb_artist or artist)
    # Only a Various Artists credit makes a compilation: a single-artist best-of or
    # remix album keeps its official credit, and local compilation inference must
    # not override one. A compilation credited to a label or DJ whose own tracks are
    # a minority is still a various-artists release.
    is_va = (
        is_various_artists(mb_artist)
        or (not mb_artist and release_data.get("is_va", False))
        or (is_compilation and bool(mb_artist) and _is_multi_artist_compilation(mb_artist, official_tracks))
    )
    rel_artist = "Various Artists" if is_va else (mb_artist or artist)
    for track in official_tracks:
        track["artist"] = track["own_artist"] or rel_artist

    recording_candidates = release_data.get("recording_candidates", [])
    positions_by_recording: Dict[str, List[int]] = {}
    track_ids: Dict[str, int] = {}
    titles_by_norm: Dict[str, List[int]] = {}
    base_counts: Dict[str, int] = {}
    official_keys = [_official_keys(track) for track in official_tracks]
    official_titles = [_parsed_titles(track["title"], track["alt_titles"]) for track in official_tracks]
    for index, track in enumerate(official_tracks):
        if track["mb_recording_id"]:
            positions_by_recording.setdefault(track["mb_recording_id"], []).append(index)
        if track["mb_track_id"]:
            track_ids[track["mb_track_id"]] = index
        titles_by_norm.setdefault(track["norm_title"], []).append(index)
        for base in {parsed["base_norm"] for parsed in official_titles[index]}:
            base_counts[base] = base_counts.get(base, 0) + 1

    # Local positions. A release without disc tags or with continuous numbering
    # ('19 …' for disc 2 track 1 of an 18/14/15 album) is flat; one MusicBrainz
    # medium of vinyl sides means a local 'disc 2' is side B.
    local_discs = {int(lt.get("disc_number") or 1) for lt in local_tracks}
    is_flat = len(official_media) > 1 and local_discs <= {1}
    single_vinyl = len(official_media) == 1 and any(
        vinyl_position(track["position_token"]) for track in official_tracks
    )
    medium_sizes = {medium["position"]: medium["track_count"] for medium in official_media}
    # Tracks before each medium in flat numbering: disc 2 of an 18/14/15 album is 19..32.
    medium_offsets: Dict[int, int] = {}
    for track in official_tracks:
        disc = int(track["disc_number"] or 1)
        offset = track["absolute_position"] - track["track_position"]
        medium_offsets[disc] = min(medium_offsets.get(disc, offset), offset)
    # The scan reads a side letter as a disc ('B2' -> disc 2), but sides are not
    # media: MusicBrainz lists 'C1' on the second LP, and every side of a single LP
    # on its only medium.
    side_media: Dict[str, int] = {}
    for track in official_tracks:
        side = vinyl_position(track["position_token"])
        if side:
            side_media.setdefault(side[0], int(track["disc_number"] or 1))
    local_keys: List[Set[Tuple[Any, ...]]] = []
    # Positions a file only might have: a disc-1 number past the first medium in a
    # folder that also has disc-tagged files is continuous numbering of another layout
    # ('19 …' as disc 2 track 1) or another edition's bonus track. Its title decides.
    title_only_keys: List[Set[Tuple[Any, ...]]] = []
    local_titles: List[Dict[str, Any]] = []
    local_numbers: List[Optional[int]] = []
    # The medium a local file is on, or None when its side names no known medium.
    local_media: List[Optional[int]] = []
    for lt in local_tracks:
        disc = int(lt.get("disc_number") or 1)
        number = _local_number(lt)
        keys: Set[Tuple[Any, ...]] = set()
        weak: Set[Tuple[Any, ...]] = set()
        side = _local_vinyl(lt, disc, number)
        if side:
            keys.add(("side",) + side)
        if number is not None:
            if not side or side[0] == "A":
                # Side B's 'disc 2' is no medium: on a double LP medium 2 is sides C/D.
                keys.add(("medium", disc, number))
            if is_flat:
                # No disc tags: '13' after a 12-track first disc is the next disc.
                keys.add(("absolute", number))
            elif not side and number > medium_sizes.get(disc, number):
                if medium_offsets.get(disc, 0) < number <= medium_offsets.get(disc, 0) + medium_sizes.get(disc, 0):
                    # Continuous numbering on its own tagged disc ('19' as disc 2 track 1).
                    keys.add(("absolute", number))
                else:
                    weak.add(("absolute", number))
            if single_vinyl and disc > 1 and not side and disc <= 8:
                keys.add(("side", chr(ord("A") + disc - 1), number))
        if side:
            local_media.append(side_media.get(side[0]) or (1 if len(official_media) == 1 else None))
        else:
            local_media.append(disc)
        local_keys.append(keys | weak)
        title_only_keys.append(weak)
        local_numbers.append(number)
        local_titles.append(parse_track_title_structure(lt.get("title") or "", is_filename=False))

    matched: Dict[int, Dict[str, Any]] = {}
    covered_by: Dict[int, Optional[str]] = {}
    used_local: Set[int] = set()
    official_recordings = set(positions_by_recording)
    official_track_ids = set(track_ids)

    # Pass 1: recording/track MBIDs for every official track before any title
    # matching, so a title cannot take a file that its MBIDs assign to another track.
    for off_index, off in enumerate(official_tracks):
        best = None
        for idx, lt in enumerate(local_tracks):
            if idx in used_local:
                continue
            by_track = bool(off["mb_track_id"] and off["mb_track_id"] in (lt.get("mb_track_ids") or []))
            by_recording = bool(off["mb_recording_id"] and off["mb_recording_id"] in (lt.get("mb_rec_ids") or []))
            if not by_track and not by_recording:
                continue
            if by_recording and not by_track and set(lt.get("mb_track_ids") or []) & official_track_ids:
                # Its track ID names another track of this edition (a repeated recording).
                continue
            at_position = bool(local_keys[idx] & official_keys[off_index])
            if by_recording and not by_track and not at_position:
                # Reserve a repeated recording for its actual position when that
                # position also appears on this edition's tracklist.
                if any(local_keys[idx] & official_keys[other]
                       for other in positions_by_recording[off["mb_recording_id"]] if other != off_index):
                    continue
            rank = (by_track, at_position, -idx)
            if best is None or rank > best[0]:
                best = (rank, idx)
        if best is not None:
            matched[off_index] = local_tracks[best[1]]
            used_local.add(best[1])

    def identified_elsewhere(off_index: int, idx: int) -> bool:
        """A file whose MBIDs name another track of this edition is not this track."""
        lt = local_tracks[idx]
        off = official_tracks[off_index]
        recordings = set(lt.get("mb_rec_ids") or [])
        if recordings & official_recordings and off["mb_recording_id"] not in recordings:
            return True
        tracks = set(lt.get("mb_track_ids") or [])
        return bool(tracks & official_track_ids and off["mb_track_id"] not in tracks)

    def reserved_title(off_index: int, idx: int) -> bool:
        """A repeated title ('Intro') belongs at the known position the file carries."""
        off = official_tracks[off_index]
        same_title = titles_by_norm[off["norm_title"]]
        if len(same_title) < 2 or normalize_text(local_tracks[idx].get("title", "")) != off["norm_title"]:
            return False
        if local_keys[idx] & official_keys[off_index]:
            return False
        return any(local_keys[idx] & official_keys[other] for other in same_title if other != off_index)

    def assign(pairs: List[Tuple[Tuple[Any, ...], int, int]]) -> None:
        for _, off_index, idx in sorted(pairs, key=lambda pair: (pair[0], pair[1], pair[2])):
            if off_index in matched or idx in used_local:
                continue
            matched[off_index] = local_tracks[idx]
            used_local.add(idx)

    # Pass 2: the file's position and a similar title (or a title-less numbered file).
    pairs = []
    for off_index in range(len(official_tracks)):
        if off_index in matched:
            continue
        for idx, lt in enumerate(local_tracks):
            if idx in used_local or not (local_keys[idx] & official_keys[off_index]):
                continue
            if identified_elsewhere(off_index, idx) or reserved_title(off_index, idx):
                continue
            if is_numeric_track_item(lt):
                # A title-less file needs a position it certainly has.
                if (local_keys[idx] - title_only_keys[idx]) & official_keys[off_index]:
                    pairs.append(((0, -1.0, -1.0), off_index, idx))
                continue
            score = _title_score(official_titles[off_index], local_titles[idx], at_position=True)
            if score is not None and score[1] >= 0.70:
                pairs.append(((0 if score[0] else 1, -score[1], -score[2]), off_index, idx))
    assign(pairs)

    # Pass 3: a highly similar title at a compatible position. Without disc tags, a
    # title unique across the whole tracklist may match another disc's track.
    pairs = []
    for off_index, off in enumerate(official_tracks):
        if off_index in matched:
            continue
        unique_title = all(base_counts.get(parsed["base_norm"], 0) == 1 for parsed in official_titles[off_index])
        for idx, lt in enumerate(local_tracks):
            if idx in used_local or is_numeric_track_item(lt):
                continue
            if identified_elsewhere(off_index, idx) or reserved_title(off_index, idx):
                continue
            agrees = bool(local_keys[idx] & official_keys[off_index])
            medium = local_media[idx]
            same_disc = medium == int(off["disc_number"] or 1)
            if not (agrees or same_disc or ((is_flat or medium is None) and unique_title)):
                continue
            score = _title_score(official_titles[off_index], local_titles[idx])
            if score is None or score[1] < 0.85:
                continue
            # A numbered file at another position is another track unless its full
            # title (version included) is exactly this one ('… (Christopher Ledger
            # remix)' is not '… (Christopher Ledger 'Hypno Tool' remix)').
            if local_numbers[idx] is not None and not agrees and not score[0]:
                continue
            pairs.append(((0 if score[0] else 1, -score[1], -score[2]), off_index, idx))
    assign(pairs)

    # Pass 4: a split album's sibling editions (another release MBID of the same
    # album) can hold the exact recording. A recording that appears once on this
    # edition is covered at any position; a repeated recording only at its own.
    used_candidates: Set[int] = set()
    candidate_positions: Dict[int, Tuple[Optional[int], Optional[int]]] = {}
    for off_index, off in enumerate(official_tracks):
        if off_index in matched or not off["mb_recording_id"]:
            continue
        repeated = len(positions_by_recording.get(off["mb_recording_id"], [])) > 1
        off_position = parse_disc_and_track_number(
            off["track_number"], meta_disc=int(off["disc_number"] or 1),
        )[:2]
        best = None
        for idx, candidate in enumerate(recording_candidates):
            if idx in used_candidates:
                continue
            if not (candidate.get("path") or candidate.get("filename")
                    or candidate.get("source") == "navidrome"):
                continue
            if off["mb_recording_id"] not in (candidate.get("mb_rec_ids") or []):
                continue
            if idx not in candidate_positions:
                candidate_positions[idx] = parse_disc_and_track_number(
                    candidate.get("track_number"), filename=candidate.get("filename"),
                    meta_disc=candidate.get("disc_number"),
                )[:2]
            position = candidate_positions[idx]
            at_position = off_position[1] is not None and position == off_position
            if repeated and not at_position:
                continue
            rank = (at_position, -idx)
            if best is None or rank > best[0]:
                best = (rank, idx)
        if best is not None:
            candidate = recording_candidates[best[1]]
            matched[off_index] = candidate
            covered_by[off_index] = candidate.get("sibling_release_id")
            used_candidates.add(best[1])

    reconciled_tracklist: List[Dict[str, Any]] = []
    for off_index, off_trk in enumerate(official_tracks):
        matched_local = matched.get(off_index)
        eff_trk_artist = (
            off_trk.get("artist")
            or (matched_local.get("artist") if matched_local else None)
            or rel_artist
        )
        row = {
            "disc_number": off_trk["disc_number"],
            "track_number": off_trk["track_number"],
            "title": off_trk["title"],
            "alt_titles": list(off_trk["alt_titles"]),
            "position_token": off_trk["position_token"],
            "track_position": off_trk["track_position"],
            "absolute_position": off_trk["absolute_position"],
            "medium_track_count": off_trk["medium_track_count"],
            "artist": eff_trk_artist,
            "mb_track_id": off_trk["mb_track_id"],
            "mb_recording_id": off_trk["mb_recording_id"],
        }
        if matched_local:
            row.update({
                "status": "found",
                "source": matched_local.get("source"),
                "remote_id": matched_local.get("remote_id"),
                "mb_track_ids": matched_local.get("mb_track_ids", []),
                "mb_rec_ids": matched_local.get("mb_rec_ids", []),
                "filename": matched_local.get("filename"),
                "path": matched_local.get("path"),
                "format": matched_local.get("format"),
                "bitrate": matched_local.get("bitrate"),
                "is_lossless": matched_local.get("is_lossless"),
                "quality_score": matched_local.get("quality_score"),
                "duration": matched_local.get("duration") or off_trk["duration"],
            })
            if off_index in covered_by:
                row["covered_by_edition"] = covered_by[off_index]
        else:
            row.update({
                "status": "missing",
                "filename": None,
                "path": None,
                "format": None,
                "bitrate": None,
                "is_lossless": None,
                "quality_score": 0,
                "duration": off_trk["duration"],
            })
        reconciled_tracklist.append(row)

    # Unmatched local files (bonus tracks, other mixes, duplicates) stay visible as
    # extras; is_extra marks that they are not official tracks.
    for idx, lt in enumerate(local_tracks):
        if idx not in used_local and (lt.get("filename") or lt.get("path")):
            reconciled_tracklist.append(
                {
                    "disc_number": lt.get("disc_number", 1) or 1,
                    "track_number": lt.get("track_number") or "-",
                    "title": lt.get("title") or lt.get("filename"),
                    "artist": lt.get("artist") or rel_artist,
                    "status": "found",
                    "is_extra": True,
                    "filename": lt.get("filename"),
                    "path": lt.get("path"),
                    "format": lt.get("format"),
                    "bitrate": lt.get("bitrate"),
                    "is_lossless": lt.get("is_lossless"),
                    "quality_score": lt.get("quality_score"),
                    "duration": lt.get("duration"),
                    "mb_recording_id": None,
                }
            )

    # Sort tracks cleanly by (disc_number, track position)
    def _audit_sort_key(t: Dict[str, Any]) -> Tuple[int, int, str]:
        dn = t.get("disc_number") or 1
        if t.get("track_position"):
            return dn, int(t["track_position"]), t.get("title") or ""
        tn_val = t.get("track_number")
        try:
            tn_int = int(re.sub(r"[^\d]", "", str(tn_val))) if tn_val else 9999
        except Exception:
            tn_int = 9999
        return dn, tn_int, t.get("title") or ""

    reconciled_tracklist.sort(key=_audit_sort_key)

    # Counts describe the official tracklist: extras keep their 'found' rows (the
    # files exist) but are neither expected nor found official tracks.
    official_rows = [t for t in reconciled_tracklist if not t.get("is_extra")]
    total_tracks = len(official_rows)
    found_count = sum(1 for t in official_rows if t["status"] == "found")
    missing_count = sum(1 for t in official_rows if t["status"] == "missing")
    completion_pct = (
        round((found_count / total_tracks * 100.0), 1) if total_tracks > 0 else 100.0
    )

    result = dict(release_data)
    result["artist"] = rel_artist
    result["album_artist"] = rel_artist
    result["is_va"] = is_va
    result["is_compilation"] = is_compilation
    result["mb_release_id"] = mb_release.get("id")
    result["mb_release_title"] = mb_release.get("title")
    result["mb_release_group_id"] = rg.get("id")
    result["mb_release_media"] = official_media
    result["mb_release_date"] = mb_release.get("date")
    result["mb_release_country"] = mb_release.get("country")
    result["mb_release_disambiguation"] = mb_release.get("disambiguation")
    result["total_tracks_expected"] = total_tracks
    result["found_count"] = found_count
    result["missing_count"] = missing_count
    result["completion_pct"] = completion_pct
    result["status"] = "has_missing" if missing_count > 0 else "complete"
    result["tracks"] = reconciled_tracklist
    result["is_audited"] = bool(official_tracks)
    result.pop("audit_error", None)
    if official_tracks and local_tracks and not used_local:
        # Not one local file is on this tracklist: the wrong release or artist.
        result["is_audited"] = False
        result["status"] = "unverified"
        result["audit_error"] = NO_MATCH_ERROR
    return result


def _search_candidates(
    mb_client: MusicBrainzClient, release_data: Dict[str, Any], local_tracks: List[Dict[str, Any]],
    force_refresh: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The best-covered same-title release that credits the local artist, or an error."""
    artist = release_data.get("artist", "")
    title = release_data.get("title", "")
    search_results = None
    if force_refresh:
        try:
            # An explicit refresh bypasses the client's cached search results.
            search_results = mb_client.search_release(
                release_title=title, artist_name=artist, limit=5, force_refresh=True,
            )
        except TypeError:
            search_results = None
    if search_results is None:
        search_results = mb_client.search_release(release_title=title, artist_name=artist, limit=5)
    norm_target_title = normalize_text(title)
    local_names = _local_credits(release_data, local_tracks)
    # A local compilation is checked by its tracks' credits after fetching: MusicBrainz
    # may credit it to a curator or label that no local track names.
    local_va = bool(release_data.get("is_va")) or is_various_artists(
        release_data.get("album_artist") or release_data.get("artist")
    )
    best: Optional[Tuple[Tuple[int, int], Dict[str, Any]]] = None
    error = None
    examined = 0
    for rank, cand in enumerate(search_results or []):
        if examined >= _MAX_SEARCH_CANDIDATES:
            break
        cand_title = normalize_text(cand.get("title", ""))
        if not (cand_title == norm_target_title or calculate_similarity(cand_title, norm_target_title) >= 0.70):
            continue
        # A search result naming another artist is skipped without fetching it;
        # a Various Artists result needs its per-track credits.
        cand_artist = format_artist_credit(cand.get("artist-credit")) or cand.get("artist-credit-phrase", "")
        if (cand_artist and local_names and not local_va and not is_various_artists(cand_artist)
                and not any(artist_credit_matches(cand_artist, name) for name in local_names)):
            error = error or "MusicBrainz found only same-title releases by other artists"
            continue
        cand_id = cand.get("id")
        if not cand_id:
            continue
        examined += 1
        mb_release = mb_client.get_release_by_id(cand_id, force_refresh=force_refresh)
        if not mb_release:
            continue
        mb_artist = format_artist_credit(mb_release.get("artist-credit")) or mb_release.get(
            "artist-credit-phrase", ""
        )
        official_tracks, _ = _official_tracklist(mb_release, mb_artist or artist)
        if not _has_artist_evidence(mb_release, official_tracks, mb_artist, release_data, local_tracks):
            error = error or "MusicBrainz found only same-title releases by other artists"
            continue
        audited = _reconcile(mb_release, release_data, local_tracks, artist)
        if not audited.get("is_audited"):
            error = error or audited.get("audit_error")
            continue
        official_found = sum(1 for track in audited["tracks"]
                             if track["status"] == "found" and not track.get("is_extra"))
        coverage = (official_found, -rank)
        if best is None or coverage > best[0]:
            best = (coverage, audited)
    if best is not None:
        return best[1], None
    return None, error


def audit_release(
    mb_client: MusicBrainzClient, release_data: Dict[str, Any], force_refresh: bool = False,
    mb_release: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Reconciles a local release against MusicBrainz to identify full tracklist,
    found tracks, and missing tracks with real official song titles.
    Supplied MusicBrainz metadata is authoritative and avoids another lookup.

    Official tracks carry the printed title ('title'), other official spellings
    ('alt_titles'), the MusicBrainz position token ('position_token', e.g. 'B2'),
    the index within the medium ('track_position'), the index across all media
    ('absolute_position') and the medium's track count ('medium_track_count').
    Local files that match no official track are listed with is_extra=True and are
    not counted as found. A release found by title search must credit the local
    artist, and any release that matches none of the local files is unverified.
    """
    artist = release_data.get("artist", "")
    title = release_data.get("title", "")
    mb_rel_id = release_data.get("mb_release_id")

    # Only consider actual local audio files on disk (ignore any prior missing placeholders)
    local_tracks = [t for t in release_data.get("tracks", []) if t.get("filename") or t.get("path") or t.get("source") == "navidrome"]

    # Defensive normalization: ensure disc_number and track_number are accurately parsed from filenames
    for lt in local_tracks:
        curr_disc = int(lt["disc_number"]) if lt.get("disc_number") else None
        if curr_disc is not None:
            lt["disc_number"] = curr_disc
        if not curr_disc or curr_disc == 1:
            p_disc, p_trk, _ = parse_disc_and_track_number(
                lt.get("track_number"),
                filename=lt.get("filename") or lt.get("path"),
                meta_disc=curr_disc,
            )
            if p_disc and p_disc > 1:
                lt["disc_number"] = p_disc
            if p_trk is not None:
                if not lt.get("track_number") or lt.get("track_number") == "-":
                    lt["track_number"] = str(p_trk)
                if lt.get("track_num_int") is None:
                    lt["track_num_int"] = p_trk

    supplied_metadata = mb_release is not None

    # 1. Look up by MBID if available
    if not supplied_metadata and mb_rel_id:
        mb_release = mb_client.get_release_by_id(mb_rel_id, force_refresh=force_refresh)

    if mb_release:
        return _reconcile(mb_release, release_data, local_tracks, artist)

    # 2. Search MusicBrainz by release title + artist if not found by ID
    error = None
    if not supplied_metadata and not is_unknown_album(title):
        audited, error = _search_candidates(mb_client, release_data, local_tracks, force_refresh)
        if audited is not None:
            return audited

    # Sequence gaps alone cannot establish MusicBrainz completeness.
    result = dict(release_data)
    result["is_audited"] = False
    result["status"] = "unverified"
    if error:
        result["audit_error"] = error
    return result
