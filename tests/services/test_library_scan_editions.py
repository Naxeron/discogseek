"""Local grouping: majority editions, stable credits, totals, and recoverable read errors."""

import os
from pathlib import Path

import mutagen
import pytest

from discogseek.core.audio import AudioMetadata
from discogseek.core.cache import UnifiedCacheManager
from discogseek.services import library_scan
from discogseek.services.library_scan import _group_releases, scan_library_releases, scan_library_tracks
from tests.core.test_audio_tag_extraction import write_flac


def meta(folder, name, album="Album", artist="Artist", album_artist="", release_ids=(), number="1"):
    return AudioMetadata(
        path=Path(folder) / name, title=Path(name).stem, artist=artist, album_artist=album_artist,
        album=album, track_number=number, mb_release_ids=set(release_ids),
    )


@pytest.mark.parametrize("minority_first", [True, False])
def test_mixed_edition_folder_takes_the_majority_edition_in_any_order(minority_first):
    folder = "/music/Machinedrum/Vapor City Archives"
    tracks = [meta(folder, f"{n:02d} Song.flac", album="Vapor City Archives", artist="Machinedrum",
                   album_artist="Machinedrum", release_ids=["e6ba0fe5"], number=str(n)) for n in range(1, 10)]
    stray = meta(folder, "04 Only 1 Way 2 Know.flac", album="Vapor City Archives", artist="Machinedrum",
                 album_artist="Machinedrum", release_ids=["4c742c51"], number="4")
    tracks = [stray] + tracks if minority_first else tracks + [stray]

    releases = list(_group_releases(tracks, Path("/music")).values())

    assert len(releases) == 1
    assert releases[0]["mb_release_id"] == "e6ba0fe5"
    assert len(releases[0]["tracks"]) == 10


def test_tied_editions_pick_the_smallest_id_in_any_order():
    folder = "/music/Novaform"
    a = meta(folder, "01 A.flac", album="Blueshift", release_ids=["c606a181"])
    b = meta(folder, "02 B.flac", album="Blueshift", release_ids=["18655c61"], number="2")

    for order in ([a, b], [b, a]):
        assert list(_group_releases(order, Path("/music")).values())[0]["mb_release_id"] == "18655c61"


def test_featured_guests_do_not_make_an_untagged_album_a_compilation():
    folder = "/music/Lone/Levitate"
    tracks = [
        meta(folder, "01 A.flac", album="Levitate", artist="Lone", number="1"),
        meta(folder, "02 B.flac", album="Levitate", artist="Lone", number="2"),
        meta(folder, "03 C.flac", album="Levitate", artist="Lone feat. Guest", number="3"),
    ]

    releases = list(_group_releases(tracks, Path("/music")).values())

    assert [(r["artist"], r["is_va"], len(r["tracks"])) for r in releases] == [("Lone", False, 3)]


@pytest.mark.parametrize("credit", ["Simon & Garfunkel", "Venetian Snares x Daniel Lanois"])
def test_untagged_collaboration_album_keeps_its_full_credit(credit):
    # Without an album-artist tag the release is named by its tracks' credit, not by the
    # first name of a collaboration, so it still merges with tagged copies and Navidrome.
    folder = f"/music/{credit}/Album"
    tracks = [meta(folder, f"{n:02d} T.flac", album="Album", artist=credit, number=str(n)) for n in (1, 2)]
    tracks.append(meta(folder, "03 T.flac", album="Album", artist=f"{credit} feat. Guest", number="3"))

    releases = list(_group_releases(tracks, Path("/music")).values())

    assert [(r["artist"], r["album_artist"], r["is_va"], len(r["tracks"])) for r in releases] == [
        (credit, credit, False, 3),
    ]


def test_two_albums_flattened_into_one_download_folder_stay_separate_albums():
    # slskd saves every remote '.../CD2' folder into the same local 'CD2' folder.
    folder = "/music/downloads/CD2"
    tracks = [
        meta(folder, "01 X.flac", album="Album X", artist="Artist A", number="1"),
        meta(folder, "02 X.flac", album="Album X", artist="Artist A", number="2"),
        meta(folder, "01 Y.flac", album="Album Y", artist="Artist B", number="1"),
        meta(folder, "02 Y.flac", album="Album Y", artist="Artist B", number="2"),
    ]

    releases = sorted(_group_releases(tracks, Path("/music")).values(), key=lambda r: r["title"])

    assert [(r["artist"], r["title"], r["is_va"]) for r in releases] == [
        ("Artist A", "Album X", False), ("Artist B", "Album Y", False),
    ]


def test_untagged_folder_of_many_artists_is_still_a_compilation():
    folder = "/music/Mixed/Compilation"
    tracks = [meta(folder, f"{n:02d} T.flac", album="Compilation", artist=f"Artist {n}", number=str(n))
              for n in range(1, 5)]

    releases = list(_group_releases(tracks, Path("/music")).values())

    assert [(r["artist"], r["is_va"]) for r in releases] == [("Various Artists", True)]


def test_track_total_tag_reveals_trailing_missing_tracks(tmp_path):
    library = tmp_path / "Library"
    album = library / "Artist" / "Album"
    album.mkdir(parents=True)
    write_flac(album / "03 Third.flac", {
        "ALBUM": "Album", "ARTIST": "Artist", "ALBUMARTIST": "Artist", "TITLE": "Third",
        "TRACKNUMBER": "3", "TRACKTOTAL": "12",
    })
    cache = UnifiedCacheManager(tmp_path / "cache.db")

    for _ in range(2):  # fresh read, then the cached row
        release = scan_library_releases(cache, library_dir=library)[0]
        assert release["total_tracks_expected"] == 12
        assert sum(t["status"] == "missing" for t in release["tracks"]) == 11


def test_failed_read_is_retried_on_the_next_scan(tmp_path, monkeypatch):
    library = tmp_path / "Library"
    album = library / "Real Artist" / "Some Folder"
    album.mkdir(parents=True)
    path = write_flac(album / "01 Song.flac", {
        "ALBUM": "Real Album", "ARTIST": "Real Artist", "TITLE": "Song", "TRACKNUMBER": "1",
        "MUSICBRAINZ_ALBUMID": "real-release",
    })
    real_file = mutagen.File
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("Input/output error")
        return real_file(*args, **kwargs)

    monkeypatch.setattr(mutagen, "File", flaky)
    cache = UnifiedCacheManager(tmp_path / "cache.db")

    during = scan_library_tracks(cache, library)
    after = scan_library_tracks(cache, library)
    cached = scan_library_tracks(cache, library)

    assert [(m.album, m.mb_release_ids) for m in during] == [("Some Folder", set())]
    assert [(m.album, m.mb_release_ids) for m in after] == [("Real Album", {"real-release"})]
    assert [(m.album, m.mb_release_ids) for m in cached] == [("Real Album", {"real-release"})]
    assert len(calls) == 2
    assert cache.get_audio_metadata(path) is not None


def test_cached_album_artist_lottery_is_reread_and_the_album_rejoined(tmp_path):
    library = tmp_path / "Library"
    album = library / "Machine Drum" / "Bidnezz"
    album.mkdir(parents=True)
    tags = {"ALBUMARTIST": "Machine Drum", "ALBUM ARTIST": "Machinedrum", "ALBUM": "Bidnezz",
            "ARTIST": "Machine Drum", "MUSICBRAINZ_ALBUMID": "a07aa047"}
    paths = [write_flac(album / f"{n:02d} {title}.flac", dict(tags, TITLE=title, TRACKNUMBER=str(n)))
             for n, title in ((11, "Inner Outer"), (12, "Wiggle"))]
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    # Rows frozen by an older process that listed 'ALBUM ARTIST' first for one file.
    cache.store_audio_metadata_batch([
        AudioMetadata(path=paths[0], title="Inner Outer", artist="Machine Drum", album_artist="Machine Drum",
                      album="Bidnezz", track_number="11", mb_release_ids={"a07aa047"}, extractor_version=1),
        AudioMetadata(path=paths[1], title="Wiggle", artist="Machine Drum", album_artist="Machinedrum",
                      album="Bidnezz", track_number="12", mb_release_ids={"a07aa047"}, extractor_version=1),
    ])

    releases = scan_library_releases(cache, library_dir=library)

    assert [(r["artist"], len([t for t in r["tracks"] if t["status"] == "found"])) for r in releases] == [
        ("Machine Drum", 2),
    ]
    assert all(cache.get_audio_metadata(path).extractor_version >= 2 for path in paths)


def test_consistent_legacy_rows_are_not_reread(tmp_path, monkeypatch):
    library = tmp_path / "Library"
    album = library / "Artist" / "Album"
    album.mkdir(parents=True)
    path = album / "01 Song.flac"
    path.write_bytes(b"not read again")
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    cache.store_audio_metadata_batch([AudioMetadata(
        path=path, title="Song", artist="Artist", album_artist="Artist", album="Album",
        track_number="1", extractor_version=1, duration=200.0,
    )])
    monkeypatch.setattr(mutagen, "File", lambda *_a, **_k: pytest.fail("unexpected re-read"))

    assert [m.title for m in scan_library_tracks(cache, library)] == ["Song"]


def test_walk_order_of_the_mount_does_not_change_the_scan_order(tmp_path, monkeypatch):
    library = tmp_path / "Library"
    for folder in ("B", "A"):
        (library / folder).mkdir(parents=True)
        for name in ("02 Two.flac", "01 One.flac"):
            (library / folder / name).write_bytes(b"x")
    real_walk = os.walk

    def server_order(top, *args, **kwargs):
        for root, dirs, files in real_walk(top, *args, **kwargs):
            dirs.sort(reverse=True)
            yield root, dirs, sorted(files, reverse=True)

    monkeypatch.setattr(library_scan.os, "walk", server_order)
    monkeypatch.setattr(mutagen, "File", lambda *_a, **_k: None)

    paths = [m.path.relative_to(library).as_posix() for m in scan_library_tracks(UnifiedCacheManager(
        tmp_path / "cache.db"), library)]

    assert paths == ["A/01 One.flac", "A/02 Two.flac", "B/01 One.flac", "B/02 Two.flac"]


def test_legacy_row_that_cannot_be_reread_keeps_its_cached_tags(tmp_path, monkeypatch):
    # The folder's cached album artists disagree, so its legacy rows are read again; a
    # file the network mount cannot stat right then is still part of this scan.
    folder = tmp_path / "Library" / "Machine Drum" / "Bidnezz"
    rows = [
        AudioMetadata(path=folder / "11 Inner Outer.flac", title="Inner Outer", artist="Machine Drum",
                      album_artist="Machine Drum", album="Bidnezz", track_number="11", extractor_version=1),
        AudioMetadata(path=folder / "12 Wiggle.flac", title="Wiggle", artist="Machine Drum",
                      album_artist="Machinedrum", album="Bidnezz", track_number="12", extractor_version=1),
    ]
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    monkeypatch.setattr(cache, "get_audio_metadata_batch", lambda paths: {meta.path: meta for meta in rows})
    monkeypatch.setattr(library_scan.os, "walk", lambda top: iter([(str(folder), [], [p.path.name for p in rows])]))
    monkeypatch.setattr(library_scan.Path, "exists", lambda self: True)
    monkeypatch.setattr(mutagen, "File", lambda *_a, **_k: pytest.fail("a file that cannot be stat'ed is not read"))

    tracks = scan_library_tracks(cache, tmp_path / "Library")

    assert sorted(meta.title for meta in tracks) == ["Inner Outer", "Wiggle"]
