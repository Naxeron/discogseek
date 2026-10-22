"""End to end: audited rows drive the downloader, and the files slskd saves verify those rows.

The audit's official rows (printed and recording titles, side tokens, the index within
the medium and across the release) are the downloader's only description of a missing
track. After the transfers finish, slskd saves each file as
'downloads/<remote folder>/<basename>'; a library scan covering that folder must mark
exactly the requested rows found, never another version of the recording.
"""

import struct
from pathlib import Path
from unittest.mock import MagicMock

from mutagen.flac import FLAC

from discogseek.cli.browser import download_release
from discogseek.clients.slskd import SlskdClient
from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_browser import LibraryBrowserService
from tests.helpers.slskd026 import FakeSlskd026


def write_flac(path: Path, seconds: float, **tags) -> Path:
    """A FLAC file holding only STREAMINFO (with its length) and the given tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    info = bytearray(34)
    struct.pack_into(">HH", info, 0, 4096, 4096)
    samples = int(seconds * 44100)
    info[10:18] = ((44100 << 44) | (1 << 41) | (15 << 36) | samples).to_bytes(8, "big")
    path.write_bytes(b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + bytes(info))
    flac = FLAC(str(path))
    for key, value in tags.items():
        flac[key] = value
    flac.save()
    return path


class AnyQuery(dict):
    """Every search text finds the same peers: query wording is not under test here."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    def get(self, key, default=None):
        return self.responses, None


def peer_files(username, folder, files):
    return {
        "username": username, "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 5_000_000,
        "fileCount": len(files), "lockedFileCount": 0, "lockedFiles": [],
        "files": [{"filename": f"{folder}\\{name}", "size": 30_000_000, "extension": "flac", "length": length,
                   "bitDepth": 16, "sampleRate": 44100, "isLocked": False} for name, length in files],
    }


def official(number, position, title, seconds, printed=None):
    track = {
        "id": f"track-{number}", "number": str(number), "position": str(position), "length": str(seconds * 1000),
        "recording": {"id": f"rec-{number}", "title": title, "length": str(seconds * 1000)},
    }
    if printed:
        track["title"] = printed
    return track


def mb_release(release_id, media, formats):
    return {
        "id": release_id, "title": "Night Signals",
        "artist-credit": [{"name": "Lone", "artist": {"name": "Lone"}}],
        "release-group": {"id": "group", "primary-type": "Album"},
        "medium-list": [{"position": str(disc), "format": formats, "track-list": tracks}
                        for disc, tracks in enumerate(media, 1)],
    }


def library_file(music, name, seconds, release_id, number, disc=1, title=None):
    tags = {
        "TITLE": title or name.split(" ", 1)[1], "ARTIST": "Lone", "ALBUMARTIST": "Lone", "ALBUM": "Night Signals",
        "TRACKNUMBER": str(number), "DISCNUMBER": str(disc), "MUSICBRAINZ_ALBUMID": release_id,
        "MUSICBRAINZ_TRACKID": f"rec-{number}", "MUSICBRAINZ_RELEASETRACKID": f"track-{number}",
    }
    return write_flac(music / "Library" / "Lone" / "Night Signals" / f"{name}.flac", seconds, **tags)


def arrive(music, transfer, seconds, **tags):
    """Save a finished transfer where slskd puts it: downloads/<remote folder>/<basename>."""
    remote = transfer["filename"].replace("\\", "/").split("/")
    return write_flac(music / "downloads" / remote[-2] / remote[-1], seconds, **tags)


def browser_for(tmp_path, monkeypatch, release, responses):
    mb = MagicMock()
    mb.get_release_by_id.return_value = release
    mb.search_release.return_value = []
    client = SlskdClient(base_url="http://slskd.test", api_key="key")
    slskd = FakeSlskd026().install(monkeypatch, client)
    slskd.plans = AnyQuery(responses)
    service = LibraryReleaseService(mb_client=mb, slskd_client=client,
                                    cache_manager=UnifiedCacheManager(tmp_path / "cache.db"))
    return LibraryBrowserService(service, navidrome_scanner=None), slskd


def rows(audited):
    return [(t["position_token"], t["title"], t["status"]) for t in audited["tracks"] if not t.get("is_extra")]


def test_vinyl_rows_are_downloaded_by_sequence_and_recording_title_then_verified(tmp_path, monkeypatch):
    release = mb_release("vinyl", [[
        official("A1", 1, "Alpha", 200), official("A2", 2, "Beta", 210),
        official("B1", 3, "Gamma (Four Tet mix)", 300, printed="Gamma (Four Tet remix)"),
        official("B2", 4, "Delta", 180),
    ]], '12" Vinyl')
    music = tmp_path / "music"
    library_file(music, "A1 Alpha", 200, "vinyl", "A1")
    # A CD rip numbers the sides' tracks 1-4 and names B1 by its recording title. The
    # unversioned 'Gamma' at B1's number is another version of the recording.
    cdrip = peer_files("cdrip", "@@music\\Lone\\Lone - Night Signals (2011)", [
        ("02 Lone - Beta.flac", 210), ("03 Lone - Gamma (Four Tet mix).flac", 300), ("04 Lone - Delta.flac", 180),
    ])
    decoy = peer_files("decoy", "@@other\\Lone - Night Signals", [("03 Lone - Gamma.flac", 240)])
    browser, slskd = browser_for(tmp_path, monkeypatch, release, [decoy, cdrip])

    selected = next(iter(browser.iter_releases(library_dir=music)))
    missing = [t for t in selected["tracks"] if t["status"] == "missing"]
    assert [(t["position_token"], t["track_position"], t["absolute_position"], t["medium_track_count"])
            for t in missing] == [("A2", 2, 2, 4), ("B1", 3, 3, 4), ("B2", 4, 4, 4)]
    assert missing[1]["alt_titles"] == ["Gamma (Four Tet mix)"]

    _, result = download_release(browser, selected, music)

    assert sorted(t["title"] for t in result["queued_tracks"]) == ["Beta", "Delta", "Gamma (Four Tet remix)"]
    assert sorted(transfer["filename"].rsplit("\\", 1)[1] for transfer in slskd.transfers) == [
        "02 Lone - Beta.flac", "03 Lone - Gamma (Four Tet mix).flac", "04 Lone - Delta.flac",
    ]

    # The transfers finish with the peer's own tags (CD numbering, no MusicBrainz IDs);
    # the user's other copy of the unversioned 'Gamma' sits in the same downloads folder.
    for transfer in slskd.transfers:
        stem = transfer["filename"].rsplit("\\", 1)[1][:-5]
        number, title = stem.split(" Lone - ")
        arrive(music, transfer, {"Beta": 210, "Delta": 180}.get(title, 300), TITLE=title, ARTIST="Lone",
               ALBUM="Night Signals", ALBUMARTIST="Lone", TRACKNUMBER=number)
    write_flac(music / "downloads" / "Lone - Night Signals" / "03 Lone - Gamma.flac", 240,
               TITLE="Gamma", ARTIST="Lone", ALBUM="Night Signals", ALBUMARTIST="Lone", TRACKNUMBER="3")

    fresh = browser.refresh_release(selected, library_dir=music)

    assert rows(fresh) == [("A1", "Alpha", "found"), ("A2", "Beta", "found"),
                           ("B1", "Gamma (Four Tet remix)", "found"), ("B2", "Delta", "found")]
    b1 = next(t for t in fresh["tracks"] if t.get("position_token") == "B1")
    assert b1["path"].endswith("03 Lone - Gamma (Four Tet mix).flac")
    assert [t["title"] for t in fresh["tracks"] if t.get("is_extra")] == ["Gamma"]
    assert (fresh["found_count"], fresh["missing_count"]) == (4, 0)


def test_multi_disc_rows_are_downloaded_by_flat_numbers_then_verified(tmp_path, monkeypatch):
    release = mb_release("double", [
        [official(1, 1, "Alpha", 200), official(2, 2, "Beta", 210)],
        [official(1, 1, "Gamma", 300), official(2, 2, "Delta", 180)],
    ], "CD")
    # Track IDs must be unique across media.
    for disc, medium in enumerate(release["medium-list"], 1):
        for track in medium["track-list"]:
            track["id"] = f"track-{disc}-{track['number']}"
            track["recording"]["id"] = f"rec-{disc}-{track['number']}"
    music = tmp_path / "music"
    for number, title, seconds in ((1, "Alpha", 200), (2, "Beta", 210)):
        path = library_file(music, f"1-0{number} {title}", seconds, "double", number)
        flac = FLAC(str(path))
        flac["MUSICBRAINZ_TRACKID"] = f"rec-1-{number}"
        flac["MUSICBRAINZ_RELEASETRACKID"] = f"track-1-{number}"
        flac.save()
    # A single-folder rip numbers disc 2 as 03 and 04; a live 'Gamma' is another recording.
    web = peer_files("web", "@@web\\Lone - Night Signals [WEB FLAC]", [
        ("03. Gamma.flac", 300), ("04. Delta.flac", 180),
    ])
    live = peer_files("live", "@@live\\Lone - Night Signals", [("01. Gamma (Live).flac", 300)])
    browser, slskd = browser_for(tmp_path, monkeypatch, release, [live, web])

    selected = next(iter(browser.iter_releases(library_dir=music)))
    missing = [t for t in selected["tracks"] if t["status"] == "missing"]
    assert [(t["disc_number"], t["track_position"], t["absolute_position"], t["medium_track_count"])
            for t in missing] == [(2, 1, 3, 2), (2, 2, 4, 2)]

    _, result = download_release(browser, selected, music)

    assert sorted(transfer["filename"].rsplit("\\", 1)[1] for transfer in slskd.transfers) == [
        "03. Gamma.flac", "04. Delta.flac",
    ]
    for transfer in slskd.transfers:
        number, title = transfer["filename"].rsplit("\\", 1)[1][:-5].split(". ")
        arrive(music, transfer, 300 if title == "Gamma" else 180, TITLE=title, ARTIST="Lone",
               ALBUM="Night Signals", ALBUMARTIST="Lone", TRACKNUMBER=number)
    write_flac(music / "downloads" / "Lone - Night Signals" / "01. Gamma (Live).flac", 300,
               TITLE="Gamma (Live)", ARTIST="Lone", ALBUM="Night Signals", ALBUMARTIST="Lone", TRACKNUMBER="1",
               DISCNUMBER="2")

    fresh = browser.refresh_release(selected, library_dir=music)

    assert [(t["disc_number"], t["title"], t["status"]) for t in fresh["tracks"] if not t.get("is_extra")] == [
        (1, "Alpha", "found"), (1, "Beta", "found"), (2, "Gamma", "found"), (2, "Delta", "found"),
    ]
    gamma = next(t for t in fresh["tracks"] if t["title"] == "Gamma")
    assert gamma["path"].endswith("03. Gamma.flac")
    assert [t["title"] for t in fresh["tracks"] if t.get("is_extra")] == ["Gamma (Live)"]


def test_untagged_downloads_in_a_folder_named_after_the_album_verify_their_rows(tmp_path, monkeypatch):
    # Peer files without any tags: slskd saved 'downloads/Night Signals/02 Beta.flac'. The
    # scan knows no artist for them; position, title and length identify the rows.
    release = mb_release("cd", [[
        official(1, 1, "Alpha", 200), official(2, 2, "Beta", 210), official(3, 3, "Gamma", 300),
    ]], "CD")
    music = tmp_path / "music"
    library_file(music, "01 Alpha", 200, "cd", 1)
    browser, _ = browser_for(tmp_path, monkeypatch, release, [])
    selected = next(iter(browser.iter_releases(library_dir=music)))
    assert selected["missing_count"] == 2

    write_flac(music / "downloads" / "Night Signals" / "02 Beta.flac", 207)
    write_flac(music / "downloads" / "Night Signals" / "03 Gamma.flac", 296)

    fresh = browser.refresh_release(selected, library_dir=music)

    assert [(t["title"], t["status"]) for t in fresh["tracks"]] == [
        ("Alpha", "found"), ("Beta", "found"), ("Gamma", "found"),
    ]
    assert fresh["tracks"][1]["path"].endswith("downloads/Night Signals/02 Beta.flac")
