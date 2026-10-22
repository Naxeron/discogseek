"""Artist catalogs: related artists are not aliases, release groups keep their album,
and release searches are cached."""

from unittest.mock import MagicMock

import musicbrainzngs
from discogseek.clients.musicbrainz import ArtistCatalog, MusicBrainzClient
from discogseek.services.reconciler import DiscographyReconciler


def squarepusher(relations, credit_extra=None):
    credit = [{"artist": {"id": "sqp", "name": "Squarepusher"}, "name": "Squarepusher"}]
    if credit_extra:
        credit = [dict(credit[0], joinphrase=" presents "),
                  {"artist": {"id": "band", "name": credit_extra}, "name": credit_extra}]
    return ArtistCatalog({
        "artist": {"id": "sqp", "name": "Squarepusher", "artist-relation-list": relations},
        "releases_artist": [{
            "id": "uv", "title": "Ultravisitor", "artist-credit": credit,
            "release-group": {"id": "rg-uv", "title": "Ultravisitor", "primary-type": "Album"},
            "medium-list": [{"position": "1", "track-list": [
                {"id": "t1", "number": "1", "title": "Iambic 9 Poetry",
                 "recording": {"id": "r1", "title": "Iambic 9 Poetry"}, "artist-credit": credit},
            ]}],
        }],
        "releases_track_artist": [], "recordings": [],
    })


def band_file():
    return {
        "path": "/music/Shobaleader One/Elektrac/04 Iambic 9 Poetry.flac",
        "filename": "04 Iambic 9 Poetry.flac", "title": "Iambic 9 Poetry",
        "norm_title": "iambic 9 poetry", "album": "Elektrac", "norm_album": "elektrac",
        "artists": ["Shobaleader One"], "track_number": "4", "mb_track_ids": set(),
        "mb_rec_ids": set(), "mb_artist_ids": set(), "mb_release_ids": set(), "source": "local",
    }


RELATIONS = [
    {"type": "member of band", "direction": "forward", "artist": {"name": "Shobaleader One"}},
    {"type": "named after artist", "direction": "backward", "artist": {"name": "Venetian Hooves"}},
]


def test_band_and_tribute_relations_are_not_aliases():
    catalog = squarepusher(RELATIONS)

    assert "shobaleader one" not in catalog.aliases
    assert "venetian hooves" not in catalog.aliases
    assert {"Shobaleader One", "Venetian Hooves"} <= catalog.related_names
    assert {detail["alias"] for detail in catalog.alias_details} >= {"Shobaleader One", "Venetian Hooves"}
    found, _ = DiscographyReconciler(catalog, [band_file()]).reconcile()
    assert found == []


def test_related_name_in_the_artists_own_credits_is_an_alias():
    catalog = squarepusher(RELATIONS, credit_extra="Shobaleader One")

    assert "shobaleader one" in catalog.aliases
    assert "venetian hooves" not in catalog.aliases
    found, _ = DiscographyReconciler(catalog, [band_file()]).reconcile()
    assert [match["mb_track"]["title"] for match in found] == ["Iambic 9 Poetry"]


def test_performance_name_relation_stays_an_alias():
    catalog = squarepusher([{"type": "is person", "artist": {"name": "Tom Jenkinson"}}])

    assert "tom jenkinson" in catalog.aliases


def edition(release_id, title, recordings):
    return {
        "id": release_id, "title": title,
        "release-group": {"id": "rg-s16", "title": "Selection Sixteen", "primary-type": "Album"},
        "medium-list": [{"position": "1", "track-list": [
            {"id": f"{release_id}-{rec}", "number": str(n), "title": f"Song {rec}",
             "recording": {"id": rec, "title": f"Song {rec}"}}
            for n, rec in enumerate(recordings, 1)
        ]}],
    }


def test_promo_edition_seen_first_does_not_hide_its_album():
    catalog = ArtistCatalog({
        "artist": {"id": "sqp", "name": "Squarepusher"},
        "releases_artist": [
            edition("promo", "Anti-Greylord Protection Scheme Prelude", ["r1", "r2"]),
            edition("album", "Selection Sixteen", ["r1", "r2", "r3", "r4"]),
        ],
        "releases_track_artist": [], "recordings": [],
    })

    groups = [release for release in catalog.releases if release["release_group_id"] == "rg-s16"]
    assert [(release["id"], release["title"]) for release in groups] == [("album", "Selection Sixteen")]
    assert {track["release_title"] for track in catalog.tracks if track["recording_ids"] & {"r3", "r4"}} == {
        "Selection Sixteen",
    }


def test_group_titled_edition_seen_first_stays_the_representative():
    catalog = ArtistCatalog({
        "artist": {"id": "sqp", "name": "Squarepusher"},
        "releases_artist": [
            edition("album", "Selection Sixteen", ["r1", "r2"]),
            edition("promo", "Anti-Greylord Protection Scheme Prelude", ["r1"]),
        ],
        "releases_track_artist": [], "recordings": [],
    })

    assert [release["id"] for release in catalog.releases] == ["album"]


def test_release_searches_are_cached_and_failures_are_not(tmp_path, monkeypatch):
    search = MagicMock(return_value={"release-list": [{"id": "edition", "title": "Album"}]})
    monkeypatch.setattr(musicbrainzngs, "search_releases", search)
    client = MusicBrainzClient(cache_dir=tmp_path)

    first = client.search_release("Album", "Artist")
    second = MusicBrainzClient(cache_dir=tmp_path).search_release("Album", "Artist")

    assert first == second == [{"id": "edition", "title": "Album"}]
    assert search.call_count == 1
    client.search_release("Album", "Artist", force_refresh=True)
    assert search.call_count == 2

    search.reset_mock()
    search.side_effect = musicbrainzngs.WebServiceError("offline")
    assert client.search_release("Other", "Artist") == []
    search.side_effect = None
    search.return_value = {"release-list": [{"id": "other", "title": "Other"}]}
    assert client.search_release("Other", "Artist") == [{"id": "other", "title": "Other"}]


def test_uncached_client_always_searches(tmp_path, monkeypatch):
    search = MagicMock(return_value={"release-list": []})
    monkeypatch.setattr(musicbrainzngs, "search_releases", search)
    client = MusicBrainzClient(cache_dir=tmp_path, use_cache=False)

    client.search_release("Album", "Artist")
    client.search_release("Album", "Artist")

    assert search.call_count >= 2
    assert not (tmp_path / "release_search_cache.json").exists()


def test_title_only_fallback_after_a_failed_artist_search_is_not_cached(tmp_path, monkeypatch):
    # The artist query failed (network), so the title-only results are not this
    # artist's answer: they are returned for this scan but searched again next time.
    def search(query=None, release=None, artist=None, limit=5):
        if query and "artist:" in query or artist:
            raise musicbrainzngs.WebServiceError("offline")
        return {"release-list": [{"id": "other-artist", "title": "Album"}]}

    calls = MagicMock(side_effect=search)
    monkeypatch.setattr(musicbrainzngs, "search_releases", calls)
    client = MusicBrainzClient(cache_dir=tmp_path)

    assert client.search_release("Album", "Artist") == [{"id": "other-artist", "title": "Album"}]
    first_calls = calls.call_count
    client.search_release("Album", "Artist")

    assert calls.call_count == 2 * first_calls
