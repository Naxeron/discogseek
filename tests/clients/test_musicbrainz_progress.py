"""MusicBrainz fetching announces slow requests and preserves cached catalogs."""

import json
import logging

import pytest

from discogseek.clients.musicbrainz import MusicBrainzClient, musicbrainzngs


def test_discography_reports_stages_before_requests_and_page_counts(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="discogseek.clients.musicbrainz")
    calls = []

    def artist_details(mbid, **kwargs):
        assert caplog.messages[-1] == "Fetching MusicBrainz artist details and aliases..."
        return {"artist": {"id": mbid, "name": "Test Artist"}}

    def page(label, item_type, **kwargs):
        offset = kwargs["offset"]
        assert caplog.messages[-1] == f"Fetching MusicBrainz {label} ({offset} loaded)..."
        calls.append((label, offset))
        return {
            f"{item_type}-list": [
                {"id": f"{label}-{index}"}
                for index in range(offset, min(offset + kwargs["limit"], 101))
            ],
            f"{item_type}-count": 101,
        }

    def releases(**kwargs):
        label = "primary releases" if "artist" in kwargs else "compilation appearances"
        return page(label, "release", **kwargs)

    monkeypatch.setattr(musicbrainzngs, "get_artist_by_id", artist_details)
    monkeypatch.setattr(musicbrainzngs, "browse_releases", releases)
    monkeypatch.setattr(musicbrainzngs, "browse_recordings", lambda **kwargs: page("recordings", "recording", **kwargs))
    monkeypatch.setattr(musicbrainzngs, "browse_release_groups", lambda **kwargs: page("release groups", "release-group", **kwargs))

    catalog = MusicBrainzClient(cache_dir=tmp_path, use_cache=False).fetch_full_discography("artist-1")

    for label, key in [
        ("primary releases", "releases_artist"),
        ("compilation appearances", "releases_track_artist"),
        ("recordings", "recordings"),
        ("release groups", "release_groups"),
    ]:
        assert [offset for called_label, offset in calls if called_label == label] == [0, 100]
        assert f"Loaded 100/101 MusicBrainz {label}" in caplog.messages
        assert f"Loaded 101/101 MusicBrainz {label}" in caplog.messages
        assert len(catalog[key]) == 101


def test_cached_discography_reports_cache_without_network(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="discogseek.clients.musicbrainz")
    catalog = {"artist": {"id": "artist-1", "name": "Test Artist"}, "releases_artist": []}
    (tmp_path / "artist_artist-1.json").write_text(json.dumps(catalog), encoding="utf-8")

    def unexpected_request(*args, **kwargs):
        pytest.fail("Cached MusicBrainz catalog must not make network requests")

    for method in ("get_artist_by_id", "browse_releases", "browse_recordings", "browse_release_groups"):
        monkeypatch.setattr(musicbrainzngs, method, unexpected_request)

    assert MusicBrainzClient(cache_dir=tmp_path).fetch_full_discography("artist-1") == catalog
    assert caplog.messages == ["Loaded MusicBrainz catalog from cache (artist_artist-1.json)"]
