"""Editions of one album cover each other's recordings, whatever their numbering."""

from unittest.mock import MagicMock

from discogseek.cli.browser import BrowserModel, download_release
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_browser import LibraryBrowserService


def official(release_id, group, media, artist="Machine Drum", title="Now You Know"):
    """media: [[(number, title, recording), ...], ...] in medium order."""
    return {
        "id": release_id, "title": title,
        "artist-credit": [{"name": artist, "artist": {"name": artist}}],
        "release-group": {"id": group, "primary-type": "Album"},
        "medium-list": [{"position": str(disc), "format": "CD", "track-list": [
            {"id": f"{release_id}-{number}", "number": str(number), "position": str(index),
             "recording": {"id": recording, "title": name}}
            for index, (number, name, recording) in enumerate(tracks, 1)
        ]} for disc, tracks in enumerate(media, 1)],
    }


def file_track(folder, filename, title, disc, number, recording, source="local", artist="Machine Drum"):
    local = source == "local"
    return {
        "title": title, "artist": artist, "disc_number": disc, "track_number": str(number),
        "source": source, "status": "found",
        "path": f"/music/{folder}/{filename}" if local else "",
        "filename": filename if local else "",
        "mb_rec_ids": [recording], "mb_track_ids": [],
    }


def library_release(tracks, release_id, artist="Machine Drum", title="Now You Know", source="local"):
    return {
        "id": f"{source}-{release_id}", "artist": artist, "album_artist": artist, "title": title,
        "mb_release_id": release_id, "source": source, "tracks": tracks,
        "status": "complete", "is_audited": False, "found_count": len(tracks), "missing_count": 0,
    }


def browser_for(editions, local, remote=None):
    mb = MagicMock()
    mb.get_release_by_id.side_effect = lambda release_id, **_: editions.get(release_id)
    mb.search_release.return_value = []
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    service.scan_library_releases = MagicMock(return_value=local)
    scanner = None
    if remote is not None:
        scanner = MagicMock()
        scanner.test_connection.return_value = True
        scanner.scan_library_releases.return_value = remote
    return LibraryBrowserService(service, scanner)


def latest_by_edition(browser):
    """The browser keeps the last audit yielded for each edition."""
    rows = {}
    for row in browser.iter_releases():
        rows[row.get("mb_release_id")] = row
    return rows


# Machine Drum — Now You Know: CD fbcdb441 numbers the album 1..13, the 2×12" 5619d5aa
# numbers it 1-1..2-5 in another order. One stray file is tagged with the vinyl edition.
NYK_CD = [
    (1, "Big Beauty Hose", "big-beauty-hose"), (2, "Wishbone Be Broken", "wishbone"),
    (3, "Drums of Steel", "drums-of-steel"), (4, "Hello My Future", "hello-my-future"),
    (5, "In Between", "in-between"), (6, "Thanks Very Much", "thanks"),
    (7, "My Visuals", "my-visuals"), (8, "Monique", "monique"),
    (9, "Hihowareyoudoingiamfine", "hihow"), (10, "Reel Cleer", "reel-cleer"),
    (11, "Fury", "fury"), (12, "Are I", "are-i"), (13, "Jewlea", "jewlea"),
]
NYK_VINYL = [
    [(1, "Big Beauty Hose", "big-beauty-hose"), (2, "Hello My Future", "hello-my-future"),
     (3, "Wishbone Be Broken", "wishbone"), (4, "My Visuals", "my-visuals"), (5, "Fury", "fury")],
    [(1, "Monique", "monique"), (2, "Thanks Very Much", "thanks"), (3, "In Between", "in-between"),
     (4, "Reel Cleer", "reel-cleer"), (5, "Are I", "are-i")],
]


def now_you_know():
    editions = {
        "cd": official("cd", "nyk-group", [NYK_CD]),
        "vinyl": official("vinyl", "nyk-group", NYK_VINYL),
    }
    folder = "Machine Drum/Now You Know"
    cd_files = [file_track(folder, f"{n:02d} Machine Drum - {t}.flac", t, 1, n, rec) for n, t, rec in NYK_CD]
    stray = file_track(folder, "1-05 Fury.mp3", "Fury", 1, 5, "fury")
    # The folder's majority edition owns the local group; Navidrome lists one album
    # per release MBID, so the stray file forms its own vinyl album.
    local = [library_release(cd_files + [stray], "cd")]
    remote = [
        library_release([dict(t, source="navidrome", path="", filename="") for t in cd_files], "cd",
                        source="navidrome"),
        library_release([dict(stray, source="navidrome", path="", filename="")], "vinyl", source="navidrome"),
    ]
    return editions, local, remote


def test_now_you_know_vinyl_edition_is_complete_through_its_cd_sibling():
    editions, local, remote = now_you_know()
    browser = browser_for(editions, local, remote)

    rows = latest_by_edition(browser)

    vinyl = rows["vinyl"]
    assert vinyl["is_audited"] is True
    assert (vinyl["found_count"], vinyl["missing_count"]) == (10, 0)
    covered = {t["title"]: t.get("covered_by_edition") for t in vinyl["tracks"]}
    assert covered["Fury"] is None
    assert covered["Hello My Future"] == covered["Are I"] == "cd"
    assert rows["cd"]["missing_count"] == 0

    model = BrowserModel()
    for row in rows.values():
        model.update(row)
    assert model.visible() == []

    browser.release_service.download_missing_tracks = MagicMock()
    fresh, result = download_release(browser, vinyl, library_dir=None)
    assert fresh["missing_count"] == result["total_missing"] == 0
    browser.release_service.download_missing_tracks.assert_not_called()


# Machine Drum — Bidnezz: the CD a07aa047 (tracks 1..22) and the 2×12" e29809a1 (A1..D3)
# share recordings; the vinyl files carry the album artist spelling 'Machinedrum'.
BID_CD = [(n, f"Song {n}", f"rec-{n}") for n in range(1, 23)]
BID_VINYL_SIDES = [
    [(f"{side}{index}", f"Song {n}", f"rec-{n}") for side, index, n in (
        ("A", 1, 1), ("A", 2, 2), ("A", 3, 3), ("A", 4, 4), ("A", 5, 5), ("B", 1, 6), ("B", 2, 7),
        ("B", 3, 8), ("B", 4, 9), ("B", 5, 10), ("B", 6, 11))],
    [(f"{side}{index}", f"Song {n}", f"rec-{n}") for side, index, n in (
        ("C", 1, 13), ("C", 2, 14), ("C", 3, 17), ("C", 4, 18), ("C", 5, 19), ("D", 1, 20),
        ("D", 2, 21), ("D", 3, 22))],
]


def bidnezz(vinyl_artist="Machinedrum", vinyl_title="Bidnezz"):
    editions = {
        "cd": official("cd", "bid-group", [BID_CD], title="Bidnezz"),
        "vinyl": official("vinyl", "bid-group", BID_VINYL_SIDES, title="Bidnezz"),
    }
    folder = "Machine Drum/Bidnezz"
    cd_files = [file_track(folder, f"{n:02d} Machine Drum - {t}.flac", t, 1, n, rec) for n, t, rec in BID_CD]
    vinyl_files = []
    for disc, side in enumerate(BID_VINYL_SIDES, 1):
        for index, (_, title, recording) in enumerate(side, 1):
            if recording in ("rec-13", "rec-14", "rec-20", "rec-21"):
                continue  # C1, C2, D1 and D2 exist only as CD files
            vinyl_files.append(file_track(folder, f"{disc}-{index:02d} {title}.flac", title, disc, index,
                                          recording, artist=vinyl_artist))
    local = [library_release(cd_files, "cd", title="Bidnezz"),
             library_release(vinyl_files, "vinyl", artist=vinyl_artist, title=vinyl_title)]
    return editions, local


def test_bidnezz_vinyl_edition_is_complete_despite_an_album_artist_spelling():
    editions, local = bidnezz()

    vinyl = latest_by_edition(browser_for(editions, local))["vinyl"]

    assert (vinyl["found_count"], vinyl["missing_count"]) == (19, 0)
    covered = [(t["track_number"], t.get("covered_by_edition")) for t in vinyl["tracks"]
               if t.get("covered_by_edition")]
    assert covered == [("C1", "cd"), ("C2", "cd"), ("D1", "cd"), ("D2", "cd")]
    assert vinyl["artist"] == "Machine Drum"


def test_release_group_connects_editions_whose_local_names_differ():
    editions, local = bidnezz(vinyl_artist="MD", vinyl_title="Bidnezz LP")
    browser = browser_for(editions, local)

    rows = list(browser.iter_releases())

    vinyl_rows = [row for row in rows if row.get("mb_release_id") == "vinyl"]
    assert vinyl_rows[0]["missing_count"] == 4
    assert vinyl_rows[-1]["missing_count"] == 0

    # A later launch knows the release group before its first audit.
    _, local_again = bidnezz(vinyl_artist="MD", vinyl_title="Bidnezz LP")
    restarted = browser_for(editions, local_again)
    restarted_rows = [row for row in restarted.iter_releases() if row.get("mb_release_id") == "vinyl"]
    assert [row["missing_count"] for row in restarted_rows] == [0]


def test_recording_from_another_release_group_does_not_cover_a_missing_track():
    editions, local = bidnezz(vinyl_artist="MD", vinyl_title="Bidnezz LP")
    editions["cd"]["release-group"]["id"] = "compilation-group"

    vinyl = latest_by_edition(browser_for(editions, local))["vinyl"]

    assert vinyl["missing_count"] == 4


def test_release_groups_learned_before_an_interrupted_scan_are_kept():
    editions, local = bidnezz(vinyl_artist="MD", vinyl_title="Bidnezz LP")
    scan = browser_for(editions, local).iter_releases()
    assert {next(scan)["mb_release_id"], next(scan)["mb_release_id"]} == {"cd", "vinyl"}
    scan.close()  # the browser stopped the scan before its final pass

    _, local_again = bidnezz(vinyl_artist="MD", vinyl_title="Bidnezz LP")
    restarted = browser_for(editions, local_again)
    vinyl_rows = [row for row in restarted.iter_releases() if row.get("mb_release_id") == "vinyl"]
    assert [row["missing_count"] for row in vinyl_rows] == [0]


def test_a_refresh_between_scan_steps_keeps_the_release_group_pass():
    # The browser worker refreshes releases (finished downloads, 'r') between scan steps.
    # Saving the release groups there must not skip the scan's final sibling pass.
    editions, local = bidnezz(vinyl_artist="MD", vinyl_title="Bidnezz LP")
    browser = browser_for(editions, local)
    scan = browser.iter_releases()
    rows = [next(scan), next(scan)]

    browser.refresh_release(rows[0])
    rows.extend(scan)

    vinyl_rows = [row for row in rows if row.get("mb_release_id") == "vinyl"]
    assert vinyl_rows[0]["missing_count"] == 4
    assert vinyl_rows[-1]["missing_count"] == 0
