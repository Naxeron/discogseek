"""Whole-library audits, cross-source completeness, and fresh download inputs."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from discogseek.config import Config
from discogseek.cli.browser import download_release
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_browser import LibraryBrowserService, merge_library_releases


def track(title, number, disc=1, source="local", recording=None):
    return {
        "title": title, "artist": "Artist", "track_number": str(number),
        "disc_number": disc, "source": source, "status": "found",
        "path": f"/music/Album/{disc}-{number} {title}.flac" if source == "local" else "",
        "filename": f"{disc}-{number} {title}.flac" if source == "local" else "",
        "mb_rec_ids": [recording] if recording else [],
    }


def release(tracks, artist="Artist", title="Album", release_id="edition-1", source="local"):
    return {
        "id": f"{source}-{title}", "artist": artist, "album_artist": artist,
        "title": title, "mb_release_id": release_id, "source": source,
        "tracks": tracks, "status": "complete", "is_audited": False,
        "found_count": len(tracks), "missing_count": 0,
    }


def make_browser(local, remote=None, official=None):
    mb = MagicMock()
    mb.get_release_by_id.return_value = official or {
        "id": "edition-1", "title": "Album", "artist-credit": [{"name": "Artist"}],
        "medium-list": [{"position": 1, "track-list": [
            {"number": str(index), "recording": {"id": f"rec-{index}", "title": title}}
            for index, title in enumerate(["First", "Second", "Third"], 1)
        ]}],
    }
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    service.scan_library_releases = MagicMock(return_value=local)
    scanner = None
    if remote is not None:
        scanner = MagicMock()
        scanner.test_connection.return_value = True
        scanner.scan_library_releases.return_value = remote
    return LibraryBrowserService(service, scanner)


def test_audits_releases_without_number_gaps_and_discovers_trailing_tracks():
    browser = make_browser([release([track("First", 1)])])
    result = list(browser.iter_releases())
    assert len(result) == 1
    assert result[0]["status"] == "has_missing"
    assert result[0]["found_count"] == 1
    assert [t["title"] for t in result[0]["tracks"] if t["status"] == "missing"] == ["Second", "Third"]
    browser.release_service.slskd_client.enqueue_download.assert_not_called()


def test_merges_local_and_pathless_navidrome_tracks_before_auditing():
    browser = make_browser(
        [release([track("First", 1)], release_id=None)],
        [release([track("First", 1, source="navidrome", recording="rec-1"),
                  track("Second", 2, source="navidrome")], source="navidrome")],
    )
    result = list(browser.iter_releases())[0]
    assert result["found_count"] == 2
    assert result["missing_count"] == 1
    assert len(result["tracks"]) == 3
    assert result["tracks"][0]["path"].startswith("/music/")
    assert result["mb_release_id"] == "edition-1"


def test_refresh_picks_up_new_remote_tracks_and_keeps_selected_edition():
    browser = make_browser([release([track("First", 1)], release_id=None)], [
        release([track("Second", 2, source="navidrome")], release_id=None, source="navidrome"),
    ])
    browser.release_service.mb_client.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    selected = list(browser.iter_releases())[0]
    assert selected["missing_count"] == 1
    browser.navidrome_scanner.scan_library_releases.return_value = [release([
        track("Second", 2, source="navidrome"), track("Third", 3, source="navidrome"),
    ], release_id=None, source="navidrome")]
    refreshed = browser.refresh_release(selected, force_refresh=True)
    assert refreshed["missing_count"] == 0
    assert refreshed["found_count"] == 3
    assert refreshed["id"] == selected["id"]
    browser.release_service.mb_client.get_release_by_id.assert_called_with("edition-1", force_refresh=True)
    assert browser.release_service.scan_library_releases.call_count == 2
    assert browser.navidrome_scanner.scan_library_releases.call_count == 2
    assert all(call.kwargs["force_rescan"] is False for call in browser.release_service.scan_library_releases.call_args_list)


def test_refresh_picks_up_new_local_tracks():
    browser = make_browser([release([track("First", 1)])])
    selected = list(browser.iter_releases())[0]
    browser.release_service.scan_library_releases.return_value = [release([
        track("First", 1), track("Second", 2), track("Third", 3),
    ])]
    assert browser.refresh_release(selected)["missing_count"] == 0


def test_remote_failure_prevents_partial_results_and_download_refresh():
    browser = make_browser([release([track("First", 1)])], [])
    browser.navidrome_scanner.scan_library_releases.side_effect = RuntimeError("Remote unavailable")
    browser.release_service.audit_release = MagicMock()
    with pytest.raises(RuntimeError, match="Remote unavailable"):
        next(browser.iter_releases())
    browser.release_service.audit_release.assert_not_called()
    with pytest.raises(RuntimeError, match="Remote unavailable"):
        browser.refresh_release(release([track("First", 1)]))


def test_configured_remote_requires_credentials(monkeypatch):
    monkeypatch.setattr(Config, "NAVIDROME_URL", "http://remote.invalid")
    browser = make_browser([release([track("First", 1)])])
    with pytest.raises(ValueError, match="username and password"):
        list(browser.iter_releases())


def test_failed_musicbrainz_audit_yields_unverified_and_continues():
    browser = make_browser([release([track("First", 1)], title="A"), release([], title="B", release_id=None)])
    original_audit = browser.release_service.audit_release
    browser.release_service.audit_release = MagicMock(side_effect=[RuntimeError("Metadata offline"), original_audit(release([]))])
    result = list(browser.iter_releases())
    assert len(result) == 2
    assert result[0]["status"] == "unverified"
    assert result[0]["is_audited"] is False
    assert result[0]["audit_error"] == "Metadata offline"
    assert result[1]["is_audited"] is True


def test_no_musicbrainz_match_is_unverified_even_without_number_gaps():
    browser = make_browser([release([track("First", 1)])])
    browser.release_service.mb_client.get_release_by_id.return_value = None
    browser.release_service.mb_client.search_release.return_value = []
    result = list(browser.iter_releases())[0]
    assert result["is_audited"] is False
    assert result["status"] == "unverified"


def test_cannot_switch_to_different_edition_when_explicit_id_fails():
    browser = make_browser([release([track("First", 1)])])
    browser.release_service.mb_client.get_release_by_id.return_value["id"] = "other-edition"
    result = list(browser.iter_releases())[0]
    assert result["is_audited"] is False
    assert result["status"] == "unverified"
    assert result["mb_release_id"] == "edition-1"
    assert "different release edition" in result["audit_error"]


def test_artist_filter_includes_contributing_artists_before_remote_audit():
    remote_track = track("First", 1, source="navidrome")
    remote_track["artist"] = "Selected Artist"
    browser = make_browser([release([], title="Other", artist="Someone Else", release_id="other")], [
        release([remote_track], artist="Various Artists", source="navidrome"),
    ])
    result = list(browser.iter_releases(artist_filter="SELECTED"))
    assert len(result) == 1
    assert browser.release_service.mb_client.get_release_by_id.call_count == 1


def test_merge_keeps_repeated_recordings_on_different_discs_and_positions():
    original = release([
        track("Repeated", 1, recording="same"),
        track("Repeated", 2, recording="same"),
        track("Repeated", 1, disc=2, recording="same"),
    ])
    remote = release([track("Repeated", 1, source="navidrome", recording="same")], source="navidrome")
    merged = merge_library_releases([original, remote])
    assert len(merged) == 1
    assert len(merged[0]["tracks"]) == 3
    assert {(t["disc_number"], t["track_number"]) for t in merged[0]["tracks"]} == {(1, "1"), (1, "2"), (2, "1")}
    assert original["tracks"][0]["mb_rec_ids"] == ["same"]


def test_known_edition_conflicts_remain_separate_while_untagged_copies_unify():
    known = release([track("First", 1)])
    untagged = release([track("Second", 2)], release_id=None)
    assert len(merge_library_releases([known, untagged])) == 1
    other_edition = deepcopy(known)
    other_edition["mb_release_id"] = "edition-2"
    assert len(merge_library_releases([known, other_edition])) == 2


def test_refresh_does_not_use_tracks_from_stale_selected_release():
    browser = make_browser([release([track("First", 1)])])
    selected = list(browser.iter_releases())[0]
    browser.release_service.scan_library_releases.return_value = []
    with pytest.raises(ValueError, match="no longer in the library"):
        browser.refresh_release(selected)


def test_refresh_limits_remote_detail_lookup_to_selected_identity():
    browser = make_browser([release([track("First", 1)])], [])
    selected = list(browser.iter_releases())[0]
    browser.refresh_release(selected)
    request = browser.navidrome_scanner.scan_library_releases.call_args.kwargs
    assert request["selected_release"]["mb_release_id"] == "edition-1"
    assert request["selected_release"]["title"] == "Album"


def test_audit_combines_aliases_resolving_to_one_release_and_refresh_keeps_all_sources():
    browser = make_browser(
        [release([track("First", 1)], artist="Contributor", release_id=None)],
        [release([track("Second", 2, source="navidrome"), track("Third", 3, source="navidrome")],
                 artist="Various Artists", source="navidrome")],
    )
    mb = browser.release_service.mb_client
    mb.get_release_by_id.return_value["artist-credit"] = [{"name": "Various Artists"}]
    mb.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    rows = list(browser.iter_releases())
    complete = rows[-1]
    assert complete["mb_release_id"] == "edition-1"
    assert complete["found_count"] == 3
    assert complete["missing_count"] == 0
    assert ("contributor", "album") in complete["browser_alias_keys"]
    fresh = browser.refresh_release(complete)
    assert fresh["found_count"] == 3
    assert fresh["missing_count"] == 0
    assert len(fresh["tracks"]) == 3


def test_prioritized_refresh_resolves_local_compilation_alias_before_its_scan_audit():
    browser = make_browser(
        [release([track("First", 1)], artist="ZZ Contributor", release_id=None)],
        [release([track("Second", 2, source="navidrome"), track("Third", 3, source="navidrome")],
                 artist="Various Artists", source="navidrome")],
    )
    mb = browser.release_service.mb_client
    mb.get_release_by_id.return_value["artist-credit"] = [{"name": "Various Artists"}]
    mb.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    scan = browser.iter_releases()
    selected = next(scan)
    assert selected["browser_name_key"] == ("various artists", "album")
    assert selected["missing_count"] == 1
    fresh = browser.refresh_release(selected)
    assert fresh["found_count"] == 3
    assert fresh["missing_count"] == 0
    request = browser.navidrome_scanner.scan_library_releases.call_args.kwargs["selected_release"]
    assert ("zz contributor", "album") in request["browser_alias_keys"]


def test_prioritized_refresh_blocks_if_same_title_local_identity_is_unverified():
    browser = make_browser(
        [release([track("First", 1)], artist="ZZ Contributor", release_id=None)],
        [release([track("Second", 2, source="navidrome")], artist="Various Artists", source="navidrome")],
    )
    selected = next(browser.iter_releases())
    browser.release_service.mb_client.search_release.return_value = []
    with pytest.raises(ValueError, match="same-title local release"):
        browser.refresh_release(selected)


def browser_with_downloaded_alias():
    downloaded = [dict(track(title, number), duration=180.5)
                  for number, title in enumerate(["First", "Second"], 1)]
    local = [release([track("Third", 3)]),
             release(downloaded, artist="ZZ Label (Artist)", release_id=None)]
    browser = make_browser(local)
    mb = browser.release_service.mb_client
    mb.search_release.return_value = []
    for medium in mb.get_release_by_id.return_value["medium-list"]:
        medium["position"] = "1"
        for official in medium["track-list"]:
            official["id"] = "track-" + official["number"]
            official["length"] = "180000"
    return browser, local


@pytest.mark.parametrize("force_refresh", [False, True])
def test_download_recognizes_untagged_alias_by_full_track_metadata_without_search_match(force_refresh):
    browser, local = browser_with_downloaded_alias()
    selected = next(browser.iter_releases())
    assert selected["missing_count"] == 2
    browser.release_service.download_missing_tracks = MagicMock()
    if force_refresh:
        selected = browser.refresh_release(selected, force_refresh=True)

    fresh, result = download_release(browser, selected, library_dir=None)

    assert fresh["missing_count"] == result["total_missing"] == 0
    assert fresh["found_count"] == 3
    assert fresh["artist"] == "Artist"
    assert ("zz label artist", "album") in fresh["browser_alias_keys"]
    browser.release_service.download_missing_tracks.assert_not_called()
    assert local[1]["mb_release_id"] is None
    restarted, _ = browser_with_downloaded_alias()
    restarted.release_service.mb_client.search_release.side_effect = AssertionError("Unexpected search")
    assert list(restarted.iter_releases())[-1]["missing_count"] == 0
    restarted.release_service.mb_client.search_release.assert_not_called()


@pytest.mark.parametrize("change", [
    {"title": "First (Live)"}, {"artist": "Another Artist"}, {"duration": 200},
    {"duration": None}, {"track_number": "4"}, {"disc_number": 2},
    {"title": "01", "filename": "01.flac"}, {"mb_rec_ids": ["different-recording"]},
    {"mb_track_ids": ["different-edition-track"]},
])
def test_alias_fallback_rejects_conflicting_or_insufficient_track_metadata(change):
    browser, local = browser_with_downloaded_alias()
    local[1]["tracks"][0].update(change)
    selected = next(browser.iter_releases())

    with pytest.raises(ValueError, match="same-title local release"):
        browser.refresh_release(selected)
    assert ("zz label artist", "album") not in browser._release_aliases["edition-1"]


@pytest.mark.parametrize("edition_track_id", [None, "track-1"])
def test_single_alias_track_requires_exact_edition_track_id(edition_track_id):
    browser, local = browser_with_downloaded_alias()
    local[1]["tracks"] = local[1]["tracks"][:1]
    local[1]["tracks"][0]["mb_rec_ids"] = ["rec-1"]
    if edition_track_id:
        local[1]["tracks"][0]["mb_track_ids"] = [edition_track_id]
    selected = next(browser.iter_releases())

    if edition_track_id:
        assert browser.refresh_release(selected)["missing_count"] == 1
    else:
        with pytest.raises(ValueError, match="same-title local release"):
            browser.refresh_release(selected)


def test_alias_fallback_checks_fresh_official_tracklist_before_remembering_identity():
    browser, local = browser_with_downloaded_alias()
    selected = next(browser.iter_releases())
    official = browser.release_service.mb_client.get_release_by_id.return_value
    official["medium-list"][0]["track-list"][0]["length"] = "200000"

    with pytest.raises(ValueError, match="same-title local release"):
        browser.refresh_release(selected, force_refresh=True)

    assert ("zz label artist", "album") not in browser._release_aliases["edition-1"]
    restarted, _ = browser_with_downloaded_alias()
    assert list(restarted.iter_releases())[-1]["is_audited"] is False
    restarted.release_service.mb_client.search_release.assert_called_once()


def test_explicit_refresh_forces_musicbrainz_lookup_for_unresolved_local_alias():
    browser, _ = browser_with_downloaded_alias()
    selected = next(browser.iter_releases())
    browser.release_service.audit_release = MagicMock(wraps=browser.release_service.audit_release)

    assert browser.refresh_release(selected, force_refresh=True)["missing_count"] == 0

    alias_calls = [call for call in browser.release_service.audit_release.call_args_list
                   if call.args[0]["artist"] == "ZZ Label (Artist)"]
    assert alias_calls
    assert alias_calls[0].kwargs["force_refresh"] is True
    reference_call = next(call for call in alias_calls if not call.args[0]["tracks"])
    assert reference_call.kwargs["force_refresh"] is True


def test_verified_audit_survives_browser_and_service_restart():
    local = [release([track("First", 1)])]
    browser = make_browser(local)
    first = list(browser.iter_releases())[0]
    restarted = make_browser(deepcopy(local))
    restarted.release_service.audit_release = MagicMock(side_effect=AssertionError("Unexpected reaudit"))

    second = list(restarted.iter_releases())[0]

    assert second == first
    restarted.release_service.audit_release.assert_not_called()
    restarted.release_service.scan_library_releases.assert_called_once()
    assert isinstance(second["tracks"][0]["mb_rec_ids"], set)


def test_untagged_release_cache_survives_identity_enrichment_and_refresh():
    local = [release([track("First", 1)], release_id=None)]
    browser = make_browser(local)
    browser.release_service.mb_client.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    selected = list(browser.iter_releases())[0]
    browser.release_service.audit_release = MagicMock(side_effect=AssertionError("Unexpected reaudit"))

    assert list(browser.iter_releases())[0]["missing_count"] == 2
    refreshed = browser.refresh_release(selected)

    assert refreshed["id"] == selected["id"]
    assert refreshed["missing_count"] == 2
    browser.release_service.audit_release.assert_not_called()
    assert browser.release_service.scan_library_releases.call_count == 3


@pytest.mark.parametrize("source", ["local", "navidrome"])
@pytest.mark.parametrize("change", ["add", "remove", "metadata"])
def test_cached_audit_invalidates_changed_source_inventory(source, change):
    tracks = [track("First", 1, source=source), track("Second", 2, source=source)]
    releases = [release(tracks, source=source)]
    browser = make_browser(releases if source == "local" else [], releases if source == "navidrome" else None)
    assert list(browser.iter_releases())[0]["missing_count"] == 1
    changed = deepcopy(releases)
    if change == "add":
        changed[0]["tracks"].append(track("Third", 3, source=source))
    elif change == "remove":
        changed[0]["tracks"].pop()
    else:
        changed[0]["tracks"][0]["duration"] = 321.0
    restarted = make_browser(changed if source == "local" else [], changed if source == "navidrome" else None)

    audited = list(restarted.iter_releases())[0]

    restarted.release_service.mb_client.get_release_by_id.assert_called_once()
    assert audited["missing_count"] == {"add": 0, "remove": 2, "metadata": 1}[change]
    if change == "metadata":
        assert audited["tracks"][0]["duration"] == 321.0


def test_changed_file_invalidates_cache_even_when_scanned_tags_are_unchanged(tmp_path):
    path = tmp_path / "First.flac"
    path.write_bytes(b"first version")
    local_track = track("First", 1)
    local_track["path"] = path
    local = [release([local_track])]
    list(make_browser(local).iter_releases())
    path.write_bytes(b"changed audio contents")
    restarted = make_browser(local)

    list(restarted.iter_releases())

    restarted.release_service.mb_client.get_release_by_id.assert_called_once()


def test_cached_audits_are_independent_per_release():
    local = [release([track("First", 1)]), release([track("Second", 2)], title="Other", release_id="edition-2")]

    def configure(browser):
        official = deepcopy(browser.release_service.mb_client.get_release_by_id.return_value)
        browser.release_service.mb_client.get_release_by_id.side_effect = lambda release_id, **kwargs: dict(official, id=release_id)

    browser = make_browser(local)
    configure(browser)
    list(browser.iter_releases())
    local[1]["tracks"].append(track("Third", 3))
    restarted = make_browser(local)
    configure(restarted)

    list(restarted.iter_releases())

    restarted.release_service.mb_client.get_release_by_id.assert_called_once_with("edition-2", force_refresh=False)


def test_forced_refresh_replaces_saved_untagged_audit_across_restarts():
    local = [release([track("First", 1)], release_id=None)]
    browser = make_browser(local)
    mb = browser.release_service.mb_client
    mb.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    selected = list(browser.iter_releases())[0]
    mb.get_release_by_id.return_value["medium-list"][0]["track-list"].append({
        "number": "4", "recording": {"id": "rec-4", "title": "Fourth"},
    })

    refreshed = browser.refresh_release(selected, force_refresh=True)
    restarted = make_browser(local)
    saved = list(restarted.iter_releases())[0]

    assert refreshed["missing_count"] == saved["missing_count"] == 3
    restarted.release_service.mb_client.get_release_by_id.assert_not_called()
    restarted.release_service.mb_client.search_release.assert_not_called()
    mb.get_release_by_id.assert_called_with("edition-1", force_refresh=True)


def test_failed_forced_audit_cannot_resurrect_saved_success():
    local = [release([track("First", 1)], release_id=None)]
    browser = make_browser(local)
    browser.release_service.mb_client.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    selected = list(browser.iter_releases())[0]
    browser.release_service.audit_release = MagicMock(side_effect=RuntimeError("Metadata offline"))

    assert browser.refresh_release(selected, force_refresh=True)["is_audited"] is False
    restarted = make_browser(local)
    restarted.release_service.mb_client.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    assert list(restarted.iter_releases())[0]["is_audited"] is True

    restarted.release_service.mb_client.get_release_by_id.assert_called_once()


def test_unsuccessful_audits_are_retried_after_restart():
    local = [release([track("First", 1)])]
    browser = make_browser(local)
    browser.release_service.audit_release = MagicMock(side_effect=RuntimeError("Metadata offline"))
    assert list(browser.iter_releases())[0]["is_audited"] is False
    restarted = make_browser(local)

    assert list(restarted.iter_releases())[0]["is_audited"] is True

    restarted.release_service.mb_client.get_release_by_id.assert_called_once()


def test_compilation_alias_audits_survive_restart_and_explicit_refresh():
    local = [release([track("First", 1)], artist="Contributor", release_id=None)]
    remote = [release([track("Second", 2, source="navidrome"), track("Third", 3, source="navidrome")],
                      artist="Various Artists", source="navidrome")]
    browser = make_browser(local, remote)
    mb = browser.release_service.mb_client
    mb.get_release_by_id.return_value["artist-credit"] = [{"name": "Various Artists"}]
    mb.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    assert list(browser.iter_releases())[-1]["missing_count"] == 0
    restarted = make_browser(local, remote)
    assert list(restarted.iter_releases())[-1]["missing_count"] == 0
    restarted.release_service.mb_client.get_release_by_id.assert_not_called()
    restarted.release_service.mb_client.search_release.assert_not_called()

    # A new official tracklist must also invalidate the separately cached source
    # snapshots that the next process combines into the selected compilation.
    official = deepcopy(mb.get_release_by_id.return_value)
    official["medium-list"][0]["track-list"].append({
        "number": "4", "recording": {"id": "rec-4", "title": "Fourth"},
    })
    restarted.release_service.mb_client.get_release_by_id.return_value = official
    assert list(restarted.iter_releases(force_refresh=True))[-1]["missing_count"] == 1
    refreshed = make_browser(local, remote, official=official)
    refreshed.release_service.mb_client.search_release.return_value = [{"id": "edition-1", "title": "Album"}]

    result = list(refreshed.iter_releases())[-1]

    assert result["missing_count"] == 1
    assert ("contributor", "album") in result["browser_alias_keys"]
    refreshed.release_service.mb_client.get_release_by_id.assert_not_called()
    refreshed.release_service.mb_client.search_release.assert_not_called()


def test_reordered_remote_inventory_reuses_saved_audit():
    remote = [release([track("Second", 2, source="navidrome"), track("First", 1, source="navidrome")], source="navidrome")]
    first = list(make_browser([], remote).iter_releases())[0]
    reordered = deepcopy(remote)
    reordered[0]["tracks"].reverse()
    restarted = make_browser([], reordered)

    assert list(restarted.iter_releases())[0] == first

    restarted.release_service.mb_client.get_release_by_id.assert_not_called()


def test_prioritized_compilation_refresh_reuses_cached_local_alias_offline():
    local = [release([track("First", 1)], artist="ZZ Contributor", release_id=None)]
    remote = [release([track("Second", 2, source="navidrome"), track("Third", 3, source="navidrome")],
                      artist="Various Artists", source="navidrome")]
    browser = make_browser(local, remote)
    mb = browser.release_service.mb_client
    mb.get_release_by_id.return_value["artist-credit"] = [{"name": "Various Artists"}]
    mb.search_release.return_value = [{"id": "edition-1", "title": "Album"}]
    assert list(browser.iter_releases())[-1]["missing_count"] == 0
    restarted = make_browser(local, remote)
    restarted.release_service.audit_release = MagicMock(side_effect=RuntimeError("Metadata offline"))
    selected = next(restarted.iter_releases())
    assert selected["missing_count"] == 1

    refreshed = restarted.refresh_release(selected)

    assert refreshed["missing_count"] == 0
    restarted.release_service.audit_release.assert_not_called()
