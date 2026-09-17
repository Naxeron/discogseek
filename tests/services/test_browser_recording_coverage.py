"""Shared recordings cover split album tags without conflating release editions."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from tests.services.test_library_browser import make_browser, release, track


def recording_browser(local, remote, titles=("First", "Second", "Third"), positions=None):
    browser = make_browser(local, remote)
    positions = positions or [(1, index, f"rec-{index}") for index in range(1, len(titles) + 1)]

    def official(release_id, **kwargs):
        media = {}
        for title, (disc, number, recording) in zip(titles, positions):
            media.setdefault(disc, []).append({
                "id": f"{release_id}-track-{disc}-{number}", "number": str(number),
                "recording": {"id": recording, "title": title},
            })
        return {
            "id": release_id, "title": local[0]["title"],
            "artist-credit": [{"name": local[0]["artist"]}],
            "medium-list": [{"position": disc, "track-list": tracks} for disc, tracks in media.items()],
        }

    browser.release_service.mb_client.get_release_by_id.side_effect = official
    return browser


def by_edition(browser):
    return {row["mb_release_id"]: row for row in browser.iter_releases()}


@pytest.mark.parametrize("disc_type", [int, str], ids=["integer-disc", "musicbrainz-string-disc"])
def test_ap1_complementary_editions_are_complete_with_their_own_official_tracks(disc_type):
    titles = (
        "real fuckin' high!", "the new flesh", "get into it!", "ain't ya heard?",
        "bad idea.", "ladgen", "random preset", "roots in hell", "i'm serious.",
        "ladgen (Casketkrusher remix)", "ain't ya heard? (Hitori Tori remix)",
        "random preset (Microphyst remix)", "real fuckin' high! (Breakforce One remix)",
        "ladgen (Fat Frumos remix)",
    )
    title = "AP1 - Nineties Rave Retrospective Volume 7"
    local = [release([track(titles[3], 4, recording="rec-4")], artist="goreshit", title=title)]
    remote = [release([
        track(name, index, source="navidrome", recording=f"rec-{index}")
        for index, name in enumerate(titles, 1) if index != 4
    ], artist="GORESHIT", title=title.upper(), release_id="edition-2", source="navidrome")]
    for entry in local + remote:
        for item in entry["tracks"]:
            item["mb_track_ids"] = [f"{entry['mb_release_id']}-track-1-{item['track_number']}"]
    original = deepcopy((local, remote))
    browser = recording_browser(local, remote, titles, positions=[
        (disc_type(1), index, f"rec-{index}") for index in range(1, 15)
    ])

    rows = by_edition(browser)

    assert set(rows) == {"edition-1", "edition-2"}
    assert len({row["id"] for row in rows.values()}) == 2
    for edition, row in rows.items():
        assert row["is_audited"] is True
        assert row["status"] == "complete"
        assert row["found_count"] == len(row["tracks"]) == 14
        assert row["missing_count"] == 0
        assert [item["mb_track_id"] for item in row["tracks"]] == [
            f"{edition}-track-1-{index}" for index in range(1, 15)
        ]
        assert [item["mb_recording_id"] for item in row["tracks"]] == [
            f"rec-{index}" for index in range(1, 15)
        ]
    assert (local, remote) == original


@pytest.mark.parametrize("recording, disc, number, artist, title", [
    (None, 1, 2, "Artist", "Album"),
    ("different-recording", 1, 2, "Artist", "Album"),
    ("rec-2", 2, 2, "Artist", "Album"),
    ("rec-2", 1, 3, "Artist", "Album"),
    ("rec-2", 1, 2, "Other Artist", "Album"),
    ("rec-2", 1, 2, "Artist", "Other Album"),
], ids=["untagged", "different-recording", "different-disc", "different-position", "different-artist", "different-album"])
def test_sibling_coverage_requires_recording_position_and_album_identity(recording, disc, number, artist, title):
    local = [release([track("First", 1, recording="rec-1")])]
    remote = [release([
        track("Second", number, disc=disc, source="navidrome", recording=recording),
    ], artist=artist, title=title, release_id="edition-2", source="navidrome")]

    target = by_edition(recording_browser(local, remote, titles=("First", "Second")))["edition-1"]

    assert target["found_count"] == 1
    assert target["missing_count"] == 1
    assert target["tracks"][1]["status"] == "missing"


@pytest.mark.parametrize("edition", ["edition-1", "edition-2"])
def test_repeated_recording_only_covers_its_actual_disc_and_position(edition):
    local = [release([track("Repeated", 1, recording="same-recording")])]
    remote = [release([
        track("Repeated", 2, source="navidrome", recording="same-recording"),
    ], release_id="edition-2", source="navidrome")]
    browser = recording_browser(local, remote, titles=("Repeated",) * 3, positions=[
        (1, 1, "same-recording"), (1, 2, "same-recording"), (2, 1, "same-recording"),
    ])

    target = by_edition(browser)[edition]

    assert target["found_count"] == 2
    assert target["missing_count"] == 1
    assert [(item["disc_number"], item["track_number"], item["status"]) for item in target["tracks"]] == [
        (1, "1", "found"), (1, "2", "found"), (2, "1", "missing"),
    ]


def test_targeted_refresh_observes_sibling_recording_additions_and_removals():
    local = [release([track("First", 1, recording="rec-1")])]
    remote = [release([
        track("Third", 3, source="navidrome", recording="rec-3"),
    ], release_id="edition-2", source="navidrome")]
    browser = recording_browser(local, remote)
    selected = by_edition(browser)["edition-1"]
    assert selected["missing_count"] == 1

    remote[0]["tracks"].append(track("Second", 2, source="navidrome", recording="rec-2"))
    complete = browser.refresh_release(selected)
    assert complete["missing_count"] == 0
    assert complete["found_count"] == 3
    assert complete["id"] == selected["id"]
    assert complete["mb_release_id"] == "edition-1"

    remote[0]["tracks"].pop(0)
    refreshed = browser.refresh_release(complete)
    assert refreshed["missing_count"] == 1
    assert refreshed["found_count"] == 2
    assert refreshed["tracks"][2]["status"] == "missing"
    assert refreshed["id"] == selected["id"]


@pytest.mark.parametrize("change", ["add", "remove", "recording"])
def test_cached_audit_tracks_sibling_inventory_across_restarts(change):
    local = [release([track("First", 1, recording="rec-1")])]
    sibling_tracks = [track("Third", 3, source="navidrome", recording="rec-3")]
    if change != "add":
        sibling_tracks.append(track("Second", 2, source="navidrome", recording="rec-2"))
    remote = [release(sibling_tracks, release_id="edition-2", source="navidrome")]
    first = by_edition(recording_browser(local, remote))["edition-1"]
    assert first["missing_count"] == (1 if change == "add" else 0)
    unchanged = recording_browser(deepcopy(local), deepcopy(remote))
    unchanged.release_service.audit_release = MagicMock(side_effect=AssertionError("Unexpected reaudit"))
    assert by_edition(unchanged)["edition-1"] == first
    unchanged.release_service.audit_release.assert_not_called()

    if change == "add":
        remote[0]["tracks"].append(track("Second", 2, source="navidrome", recording="rec-2"))
    elif change == "remove":
        remote[0]["tracks"].pop()
    else:
        remote[0]["tracks"][-1]["mb_rec_ids"] = ["different-recording"]
    restarted = recording_browser(deepcopy(local), deepcopy(remote))
    refreshed = by_edition(restarted)["edition-1"]

    assert refreshed["missing_count"] == (0 if change == "add" else 1)
    assert refreshed["found_count"] == (3 if change == "add" else 2)
    assert refreshed["id"] == first["id"]


def test_changed_local_sibling_file_invalidates_target_cache_with_unchanged_tags(tmp_path):
    sibling_path = tmp_path / "Second.flac"
    sibling_path.write_bytes(b"original sibling audio")
    sibling_track = track("Second", 2, recording="rec-2")
    sibling_track["path"] = sibling_path
    local = [
        release([track("First", 1, recording="rec-1")]),
        release([sibling_track], release_id="edition-2"),
    ]
    original_inventory = deepcopy(local)
    first = by_edition(recording_browser(local, [], titles=("First", "Second")))["edition-1"]
    assert first["missing_count"] == 0
    unchanged = recording_browser(deepcopy(local), [], titles=("First", "Second"))
    unchanged.release_service.audit_release = MagicMock(side_effect=AssertionError("Unexpected reaudit"))
    assert by_edition(unchanged)["edition-1"] == first
    unchanged.release_service.audit_release.assert_not_called()

    sibling_path.write_bytes(b"changed sibling audio with a different size")
    restarted = recording_browser(deepcopy(local), [], titles=("First", "Second"))
    refreshed = by_edition(restarted)["edition-1"]

    assert local == original_inventory
    assert refreshed["found_count"] == 2
    assert refreshed["missing_count"] == 0
    restarted.release_service.mb_client.get_release_by_id.assert_any_call("edition-1", force_refresh=False)
