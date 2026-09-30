"""Album rows preserve distinct edition audits, selection, and download scopes."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from discogseek.cli.browser import (
    BrowserModel, ReleaseBrowser, album_key, equivalent_release_key, release_key, track_key,
)
from discogseek.cli.main import build_parser


def xen_editions():
    """The reported album: 45 digital tracks plus local extras, versus three CDs."""
    digital_tracks = [
        {"disc_number": 1, "track_number": str(number), "title": f"Song {number}",
         "mb_recording_id": f"recording-{number}", "mb_track_id": f"digital-{number}",
         "status": "found" if number <= 13 else "missing"}
        for number in range(1, 46)
    ]
    digital_tracks.extend(
        {"disc_number": 1, "track_number": str(number), "title": f"Local extra {number}",
         "path": f"/music/Xen Cuts/extra-{number}.flac", "status": "found"}
        for number in (46, 47)
    )
    cd_tracks = []
    recording = 0
    for disc, count in enumerate((16, 16, 15), 1):
        for number in range(1, count + 1):
            recording += 1
            cd_tracks.append({
                "disc_number": disc, "track_number": str(number),
                "title": f"Song {recording}", "mb_recording_id": f"recording-{recording}",
                "mb_track_id": f"cd-{disc}-{number}",
                "status": "found" if recording == 1 else "missing",
            })
    common = {"artist": "Various Artists", "album_artist": "Various Artists",
              "title": "Xen Cuts", "mb_release_title": "Xen Cuts",
              "is_audited": True, "mb_release_group_id": "xen-cuts-group"}
    digital = dict(common, id="local-digital", mb_release_id="digital-edition",
                   tracks=digital_tracks, found_count=15, missing_count=32,
                   mb_release_media=[{"position": 1, "format": "Digital Media", "track_count": 45}],
                   mb_release_date="2000-09-25", mb_release_country="GB",
                   mb_release_disambiguation="Digital edition")
    cds = dict(common, id="remote-cds", mb_release_id="cd-edition",
               tracks=cd_tracks, found_count=1, missing_count=46,
               mb_release_media=[{"position": index, "format": "CD", "track_count": count}
                                 for index, count in enumerate((16, 16, 15), 1)],
               mb_release_date="2000-10-02", mb_release_country="US",
               mb_release_disambiguation="Three CD edition")
    return digital, cds


def complete(release):
    result = deepcopy(release)
    for item in result["tracks"]:
        item["status"] = "found"
    result.update(missing_count=0, found_count=len(result["tracks"]))
    return result


@pytest.fixture
def terminal_keys():
    return SimpleNamespace(**{name: index for index, name in enumerate([
        "KEY_ENTER", "KEY_BACKSPACE", "KEY_BTAB", "KEY_LEFT", "KEY_RIGHT", "KEY_UP",
        "KEY_DOWN", "KEY_NPAGE", "KEY_PPAGE", "KEY_HOME", "KEY_END",
    ], 1000)})


@pytest.fixture
def tui():
    return ReleaseBrowser(build_parser().parse_args(["browse"]), service_factory=Mock())


def test_xen_cuts_has_one_album_row_and_two_distinct_edition_options():
    digital, cds = xen_editions()
    original = deepcopy((digital, cds))
    model = BrowserModel()
    model.update(cds)
    model.update(digital)

    assert album_key(digital) == album_key(cds)
    assert equivalent_release_key(digital) != equivalent_release_key(cds)
    assert model.visible() == [digital]
    assert model.selected() is digital
    assert {release_key(row) for row in model.editions(digital)} == {
        "digital-edition", "cd-edition",
    }
    assert (digital, cds) == original


@pytest.mark.parametrize("unverified", [False, True])
def test_matching_album_titles_require_a_verified_common_release_group(unverified):
    digital, cds = xen_editions()
    if unverified:
        cds["is_audited"] = False
    else:
        cds["mb_release_group_id"] = "another-album-group"
    model = BrowserModel()
    model.show_unverified = True
    model.update(digital)
    model.update(cds)

    assert album_key(digital) != album_key(cds)
    assert len(model.visible()) == 2
    assert model.editions(digital) == [digital]
    assert model.editions(cds) == [cds]


def test_equivalent_recording_editions_share_one_album_row_and_keep_both_options():
    _, cds = xen_editions()
    equivalent = deepcopy(cds)
    equivalent.update(id="other-cds", mb_release_id="other-cd-edition",
                      mb_release_country="GB", mb_release_disambiguation="UK edition")
    for item in equivalent["tracks"]:
        item["mb_track_id"] = "other-" + item["mb_track_id"]
    equivalent["tracks"][1]["status"] = "found"
    equivalent.update(found_count=2, missing_count=45)
    model = BrowserModel()
    model.update(cds)
    model.update(equivalent)

    assert equivalent_release_key(cds) == equivalent_release_key(equivalent)
    assert model.editions(cds) == [equivalent, cds]
    assert model.visible() == [equivalent]
    model.record_result(cds, {"queued_tracks": [cds["tracks"][13]]})
    assert model.track_status(equivalent, equivalent["tracks"][13]) == "queued"


def test_explicit_equivalent_edition_survives_sibling_scan_and_refresh():
    _, cds = xen_editions()
    sibling = deepcopy(cds)
    sibling.update(id="other-cds", mb_release_id="other-cd-edition")
    sibling["tracks"][1]["status"] = "found"
    sibling.update(found_count=2, missing_count=45)
    model = BrowserModel()
    model.update(cds)
    model.update(sibling)
    assert model.selected() is sibling
    model.choose_edition(cds)
    assert model.selected() is cds

    late_sibling = deepcopy(sibling)
    model.update(late_sibling)
    assert model.selected() is cds
    fresh_sibling = deepcopy(sibling)
    fresh_sibling["tracks"][2]["status"] = "found"
    fresh_sibling.update(found_count=3, missing_count=44)
    model.update(fresh_sibling, old_key=release_key(sibling))

    assert model.selected() is cds
    assert model.visible() == [cds]
    assert set(map(release_key, model.editions(cds))) == {"cd-edition", "other-cd-edition"}


def test_stale_chosen_equivalent_edition_cannot_resurrect_completed_album():
    _, cds = xen_editions()
    stale_sibling = deepcopy(cds)
    stale_sibling.update(id="other-cds", mb_release_id="other-cd-edition")
    model = BrowserModel()
    model.update(cds)
    model.update(stale_sibling)
    model.choose_edition(stale_sibling)
    assert model.selected() is stale_sibling

    model.update(complete(cds), old_key=release_key(cds))

    assert stale_sibling["missing_count"] == 46
    assert model.visible() == []
    assert model.selected() is None


def test_complete_distinct_edition_is_selectable_until_all_editions_are_complete():
    digital, cds = xen_editions()
    digital = complete(digital)
    model = BrowserModel()
    model.update(digital)
    model.update(cds)

    assert model.visible() == [cds]
    assert set(map(release_key, model.editions(cds))) == {"digital-edition", "cd-edition"}
    model.choose_edition(digital)
    assert model.selected() is digital
    assert model.visible() == [digital]
    assert model.pending(digital) == []

    model.update(complete(cds))
    assert model.visible() == []
    assert model.selected() is None


def test_explicit_edition_survives_late_scan_refresh_and_full_rescan():
    digital, cds = xen_editions()
    model = BrowserModel()
    model.update(cds)
    model.update(digital)
    assert model.selected() is digital
    model.track_index = 13
    model.choose_edition(cds)

    assert model.selected() is cds
    assert model.track_index == 0
    model.update(deepcopy(digital))
    assert model.selected() is cds
    fresh = deepcopy(cds)
    fresh["tracks"][1]["status"] = "found"
    fresh.update(found_count=2, missing_count=45)
    model.update(fresh, old_key=release_key(cds))
    assert model.selected() is fresh

    model.releases.clear()
    model.update(deepcopy(digital))
    model.selected()  # Incremental rescans show the edition already discovered.
    rescanned = deepcopy(fresh)
    model.update(rescanned)
    assert model.selected() is rescanned


def test_refresh_identity_change_preserves_explicit_edition_choice():
    digital, cds = xen_editions()
    model = BrowserModel()
    model.update(digital)
    model.update(cds)
    model.choose_edition(cds)
    fresh = deepcopy(cds)
    fresh["mb_release_id"] = "resolved-cd-edition"
    model.update(fresh, old_key=release_key(cds))

    assert release_key(cds) not in model.releases
    assert model.selected() is fresh
    model.update(deepcopy(digital))
    assert model.selected() is fresh


def test_distinct_album_editions_keep_missing_counts_and_queue_history_separate():
    digital, cds = xen_editions()
    model = BrowserModel()
    model.update(digital)
    model.update(cds)
    digital_missing = digital["tracks"][13]
    cd_missing = cds["tracks"][13]
    assert track_key(digital_missing) == track_key(cd_missing)
    model.record_result(digital, {"queued_tracks": [digital_missing]})

    assert model.track_status(digital, digital_missing) == "queued"
    assert model.track_status(cds, cd_missing) == "missing"
    assert digital_missing not in model.pending(digital)
    assert cd_missing in model.pending(cds)
    model.choose_edition(cds)
    assert model.selected()["missing_count"] == 46
    model.choose_edition(digital)
    assert model.selected()["missing_count"] == 32


def test_edition_overlay_cancel_preserves_selection(tui, terminal_keys):
    digital, cds = xen_editions()
    tui.model.update(digital)
    tui.model.update(cds)
    assert tui.model.selected() is digital

    tui._handle_key("e", terminal_keys, page_size=10)
    assert tui.overlay == "editions"
    tui._handle_key("j", terminal_keys, page_size=10)
    tui._handle_key("\x1b", terminal_keys, page_size=10)

    assert tui.overlay is None
    assert tui.model.selected() is digital
    assert tui.jobs.empty()


@pytest.mark.parametrize("update_kind", ["coverage", "insert"])
def test_open_edition_overlay_keeps_highlight_when_background_results_reorder_editions(
        tui, terminal_keys, update_kind):
    digital, cds = xen_editions()
    tui.model.update(digital)
    tui.model.update(cds)
    assert tui.model.selected() is digital
    tui._handle_key("e", terminal_keys, page_size=10)
    highlighted = tui.edition_index
    assert release_key(tui.edition_choices[highlighted]) == release_key(digital)
    if update_kind == "coverage":
        result = complete(cds)
        old_key = release_key(cds)
    else:
        result = deepcopy(cds)
        result.update(id="new-cds", mb_release_id="new-cd-edition")
        result["tracks"][-1]["mb_recording_id"] = "different-final-recording"
        for track in result["tracks"][1:30]:
            track["status"] = "found"
        result.update(found_count=30, missing_count=17)
        old_key = None
    tui.events.put(("release", (old_key, result)))

    tui._drain_events()

    assert tui.model.editions(digital)[0] is result
    assert tui.edition_index == highlighted
    assert release_key(tui.edition_choices[highlighted]) == release_key(digital)
    tui._handle_key("\n", terminal_keys, page_size=10)
    assert tui.overlay is None
    assert tui.model.selected() is digital


def test_open_edition_overlay_updates_highlighted_release_when_its_identity_changes(
        tui, terminal_keys):
    digital, cds = xen_editions()
    tui.model.update(digital)
    tui.model.update(cds)
    tui._handle_key("e", terminal_keys, page_size=10)
    highlighted = tui.edition_index
    fresh = deepcopy(digital)
    fresh["mb_release_id"] = "resolved-digital-edition"
    fresh["tracks"][13]["status"] = "found"
    fresh.update(found_count=16, missing_count=31)
    tui.events.put(("release", (release_key(digital), fresh)))

    tui._drain_events()

    assert tui.edition_index == highlighted
    assert tui.edition_choices[highlighted] is fresh
    tui._handle_key("\n", terminal_keys, page_size=10)
    assert tui.model.selected() is fresh
    assert tui.model.selected_editions[album_key(fresh)] == release_key(fresh)


def test_open_edition_overlay_keeps_album_header_when_background_updates_complete_it(
        tui, terminal_keys):
    digital, cds = xen_editions()
    other = deepcopy(digital)
    other.update(id="other", mb_release_id="other-edition", title="Another Album",
                 mb_release_group_id="other-group")
    for row in (digital, cds, other):
        tui.model.update(row)
    tui.model.choose_edition(digital)
    tui._handle_key("e", terminal_keys, page_size=10)
    for row in (digital, cds):
        tui.events.put(("release", (release_key(row), complete(row))))
    tui._drain_events()
    assert tui.model.selected() is other
    lines = {}

    def put(y, x, text, cells=None, attr=0):
        lines[y] = text

    tui._draw_overlay(put, 24, 100, SimpleNamespace(A_BOLD=1, A_REVERSE=2))

    assert any("Various Artists — Xen Cuts" in line for line in lines.values())
    assert not any("Another Album" in line for line in lines.values())
    assert any("MBID: digital-edition" in line for line in lines.values())


@pytest.mark.parametrize("move_key", ["j", "arrow"])
def test_edition_overlay_selection_dispatches_chosen_edition(tui, terminal_keys, move_key):
    digital, cds = xen_editions()
    tui.model.update(digital)
    tui.model.update(cds)
    tui.model.selected()
    tui.model.track_index = 13
    tui._handle_key("e", terminal_keys, page_size=10)
    editions = tui.model.editions(digital)
    target = editions.index(cds)
    key = terminal_keys.KEY_DOWN if move_key == "arrow" else move_key
    for _ in editions:
        if tui.edition_index == target:
            break
        tui._handle_key(key, terminal_keys, page_size=10)
    assert tui.edition_index == target
    tui._handle_key("\n", terminal_keys, page_size=10)

    assert tui.overlay is None
    assert tui.model.selected() is cds
    assert tui.model.track_index == 0
    assert tui.jobs.empty()
    tui._handle_key("d", terminal_keys, page_size=10)
    kind, (submitted, queued, only_track, dry_run) = tui.jobs.get_nowait()
    assert kind == "download"
    assert release_key(submitted) == "cd-edition"
    assert submitted["missing_count"] == 46
    assert queued == set()
    assert only_track is None
    assert not dry_run


def test_distinct_edition_download_can_wait_while_another_edition_is_active(tui):
    digital, cds = xen_editions()
    tui.model.update(digital)
    tui.model.update(cds)
    tui._download()
    tui.model.choose_edition(cds)
    tui._download()

    assert tui.jobs.qsize() == 1
    assert release_key(tui.active_download.release) == "digital-edition"
    assert len(tui.pending_downloads) == 1
    assert release_key(tui.pending_downloads[0].release) == "cd-edition"


@pytest.mark.parametrize("size", [(14, 72), (24, 100)])
def test_edition_overlay_shows_metadata_and_individual_missing_counts(tui, terminal_keys, size):
    class Screen:
        def __init__(self):
            self.rows = {}

        def getmaxyx(self):
            return size

        def erase(self):
            self.rows.clear()

        def addstr(self, y, x, text, attr):
            assert 0 <= y < size[0] and x + len(text) < size[1]
            row = self.rows.get(y, " " * size[1])
            self.rows[y] = row[:x] + text + row[x + len(text):]

        def refresh(self):
            pass

    digital, cds = xen_editions()
    tui.model.update(digital)
    tui.model.update(cds)
    tui._handle_key("e", terminal_keys, page_size=10)
    curses = SimpleNamespace(**vars(terminal_keys), A_BOLD=1, A_REVERSE=2,
                             A_DIM=4, error=RuntimeError)
    screen = Screen()
    tui._draw(screen, curses)
    drawn = "\n".join(screen.rows.values())

    assert "EDITIONS" in drawn
    for text in ("Digital", "CD", "45 tracks", "47 tracks", "32/47", "46/47"):
        assert text in drawn
    if size[1] >= 100:
        for text in ("2000-09-25", "2000-10-02", "GB", "US",
                     "Digital edition", "Three CD edition"):
            assert text in drawn
