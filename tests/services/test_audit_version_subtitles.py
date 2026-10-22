"""A bracket naming another version is not a subtitle a file may omit at its position."""

from unittest.mock import MagicMock

import pytest

from discogseek.services.library import LibraryReleaseService


def official_track(number, title, printed=None):
    track = {"id": f"track-{number}", "number": str(number),
             "recording": {"id": f"rec-{number}", "title": title}}
    if printed:
        track["title"] = printed
    return track


def audit(tracks, local):
    mb = MagicMock()
    mb.get_release_by_id.return_value = {
        "id": "edition", "title": "Album", "artist-credit": [{"name": "Artist", "artist": {"name": "Artist"}}],
        "release-group": {"id": "group", "primary-type": "Album"},
        "medium-list": [{"position": "1", "format": "CD", "track-list": tracks}],
    }
    files = [{
        "title": title, "artist": "Artist", "disc_number": 1, "track_number": str(number),
        "filename": f"{number:02d} {title}.flac", "path": f"/music/Album/{number:02d} {title}.flac",
        "status": "found", "mb_rec_ids": [], "mb_track_ids": [],
    } for number, title in local]
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    return service.audit_release({"artist": "Artist", "title": "Album", "mb_release_id": "edition", "tracks": files})


def found(audited):
    return [t["title"] for t in audited["tracks"] if t["status"] == "found" and not t.get("is_extra")]


def extras(audited):
    return [t["title"] for t in audited["tracks"] if t.get("is_extra")]


def test_original_title_does_not_fill_a_remix_row_through_its_recording_version_title():
    # Bleep: 100 Tracks 2011 #70: printed '(Die Vogel remix)', recording '(Die Vögel version)'.
    tracks = [official_track(1, "Intro"),
              official_track(2, "Welt Am Draht (Die Vögel version)", printed="Welt Am Draht (Die Vogel remix)")]

    audited = audit(tracks, [(2, "Welt Am Draht")])

    assert found(audited) == []
    assert extras(audited) == ["Welt Am Draht"]


@pytest.mark.parametrize("local_title", ["Welt Am Draht (Die Vögel version)", "Welt Am Draht (Die Vogel remix)"])
def test_either_official_title_of_the_version_fills_its_row(local_title):
    tracks = [official_track(1, "Intro"),
              official_track(2, "Welt Am Draht (Die Vögel version)", printed="Welt Am Draht (Die Vogel remix)")]

    assert found(audit(tracks, [(2, local_title)])) == ["Welt Am Draht (Die Vogel remix)"]


@pytest.mark.parametrize("official_title, local_title", [
    ("Skylla (Bleep version)", "Skylla"),
    ("Skylla", "Skylla (Bleep version)"),
    ("Skylla (Single Version)", "Skylla"),
])
def test_a_version_bracket_on_either_side_keeps_the_row_missing(official_title, local_title):
    audited = audit([official_track(1, official_title)], [(1, local_title)])

    assert found(audited) == []


def test_a_subtitle_may_still_be_omitted_at_the_files_own_position():
    audited = audit([official_track(1, "Ninja Tune (Enter the Augmenter)")], [(1, "Ninja Tune")])

    assert found(audited) == ["Ninja Tune (Enter the Augmenter)"]
