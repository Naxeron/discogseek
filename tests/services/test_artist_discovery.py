"""Artist-mode searches, search failures and the split/collaboration release audit."""

import pytest

from discogseek.clients.musicbrainz import ArtistCatalog
from discogseek.core.text import normalize_text
from tests.helpers.artist_mode import (
    ARTIST_ID,
    FakeArtistClient,
    raw_catalog,
    release,
    response,
    run_scraper,
    scraper_for,
)
from discogseek.services.soulseek import SlskdArtistScraper

MBID = "d1785218-d8f4-4a83-a33d-8cf737eef918"


def machinedrum_catalog():
    return ArtistCatalog({
        "artist": {"id": MBID, "name": "Machinedrum"},
        "releases_artist": [
            {"id": "r1", "title": "Bidnezz", "medium-list": [{"track-list": [{"id": "t1", "title": "Bidnezz"}]}]},
            {"id": "r2", "title": "Let It", "medium-list": [{"track-list": [{"id": "t2", "title": "Let It"}]}]},
        ],
        "releases_track_artist": [],
        "recordings": [],
    })


@pytest.mark.parametrize("query", [MBID, f"https://musicbrainz.org/artist/{MBID}"])
def test_artist_identifier_is_never_sent_as_search_text(query):
    scraper = SlskdArtistScraper(query, slskd_client=FakeArtistClient(), dry_run=True)
    scraper.catalog = machinedrum_catalog()

    queries = scraper._artist_search_queries()
    for rel in scraper.catalog.primary_releases:
        queries += scraper._release_search_queries(rel)

    assert queries == ["Machinedrum", "Machinedrum Bidnezz", "Bidnezz", "Machinedrum Let It", "Let It"]
    assert not any(MBID in item or "musicbrainz" in item.lower() for item in queries)


def test_only_the_artists_names_are_searched_before_releases_are_checked():
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])], aliases=["Target Alias"]), client)

    scraper._discover_soulseek_candidates()

    assert client.queries == ["Target Artist", "target alias"]


SIGNALS = [f"Signal {word}" for word in ("Alpha", "Bravo", "Charlie", "Delta")]


def folder_files(folder, extension="flac"):
    return [f"{folder}\\{number:02d} {title}.{extension}" for number, title in enumerate(SIGNALS, 1)]


def test_release_the_artist_search_shows_complete_is_not_searched_by_name(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    album = folder_files("@@a\\Music\\Target Artist\\Phonetic Dreams")
    client = FakeArtistClient(responder=lambda query: [response("peer", *album)] if query == "Target Artist" else [])

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    assert client.queries == ["Target Artist"]
    assert sorted(file["filename"] for file in result["matched_files"]) == album


def test_release_the_artist_search_does_not_show_is_searched_by_name(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    # Filed under its label: only the title finds it, and its files name the artist.
    album = [name.replace("\\0", "\\Target Artist - 0") for name in folder_files("@@a\\Music\\Night Label - Phonetic Dreams")]
    client = FakeArtistClient(responder=lambda query: [response("peer", *album)] if query == "Phonetic Dreams" else [])

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    assert client.queries == ["Target Artist", "Target Artist Phonetic Dreams", "Phonetic Dreams"]
    assert sorted(file["filename"] for file in result["matched_files"]) == album


def test_release_shown_only_in_a_lossy_copy_is_searched_by_name_for_a_lossless_one(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    lossy = folder_files("@@a\\Music\\Target Artist\\Phonetic Dreams", "mp3")
    lossless = folder_files("@@b\\Music\\Target Artist - Phonetic Dreams [FLAC]")
    client = FakeArtistClient(responder=lambda query: (
        [response("lossy", *lossy)] if query == "Target Artist"
        else [response("lossless", *lossless)] if query == "Target Artist Phonetic Dreams" else []
    ))

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    assert "Target Artist Phonetic Dreams" in client.queries
    assert sorted(file["filename"] for file in result["matched_files"]) == lossless


def test_single_collected_on_a_release_found_whole_is_not_searched_by_name(monkeypatch):
    single = release("Signal Bravo", ["Signal Bravo"], 1, recordings=["rec-0-1-2"], primary_type="Single")
    raw = raw_catalog([single, release("Phonetic Dreams", SIGNALS)])
    album = folder_files("@@a\\Music\\Target Artist\\Phonetic Dreams")
    client = FakeArtistClient(responder=lambda query: [response("peer", *album)] if query == "Target Artist" else [])

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    assert client.queries == ["Target Artist"]
    assert result["unresolved_tracks"] == []


def test_made_up_romanization_of_a_kanji_name_is_not_searched():
    client = FakeArtistClient()
    # ArtistCatalog also keeps each alias as unidecode spells it ('zuo teng huang').
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])], aliases=["佐藤晃"]), client)

    assert scraper._artist_search_queries() == ["Target Artist", "佐藤晃"]


@pytest.mark.parametrize("options,use_existing", [
    ({}, True), ({"force_refresh": True}, False), ({"refresh_searches": True}, False),
])
def test_refresh_asks_soulseek_again_instead_of_reusing_slskd_searches(options, use_existing):
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client, **options)

    scraper._discover_soulseek_candidates()

    assert [kwargs["use_existing"] for kwargs in client.search_kwargs] == [use_existing]


def test_failed_searches_are_reported():
    client = FakeArtistClient()
    client.batch_search = lambda queries, **kwargs: {
        query: ({"error": "HTTP 409: search exists", "responses": [], "searchText": query}
                if query == "Night Signals" else {"responses": []})
        for query in queries
    }
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)

    scraper._discover_soulseek_candidates()
    scraper._search_release(scraper.catalog.primary_releases[0])

    assert scraper.search_errors == [{"query": "Night Signals", "error": "HTTP 409: search exists"}]


def credit(*names_and_ids, joiner=" & "):
    parts = []
    for index, (name, artist_id) in enumerate(names_and_ids):
        part = {"artist": {"id": artist_id, "name": name}, "name": name}
        if index < len(names_and_ids) - 1:
            part["joinphrase"] = joiner
        parts.append(part)
    return parts


def split_release(release_credit, first_credit):
    return {
        "id": "split-1", "title": "Twin Signals",
        "artist-credit": release_credit,
        "release-group": {"id": "rg-split", "primary-type": "EP"},
        "medium-list": [{"position": "1", "track-list": [
            {"id": "t1", "number": "1", "position": "1", "title": "Neon Memory", "artist-credit": first_credit,
             "recording": {"id": "r1", "title": "Neon Memory"}},
            {"id": "t2", "number": "2", "position": "2", "title": "Midnight Echo",
             "artist-credit": credit(("Other Artist", "other")), "recording": {"id": "r2", "title": "Midnight Echo"}},
        ]}],
    }


def inventory(album_artist):
    return [{
        "path": f"/music/{album_artist}/Twin Signals/0{number} {title}.flac", "filename": f"0{number} {title}.flac",
        "title": title, "norm_title": normalize_text(title), "album": "Twin Signals", "norm_album": "twin signals",
        "album_artist": album_artist, "artists": [artist], "track_number": str(number), "disc_number": 1,
        "mb_track_ids": set(), "mb_rec_ids": set(), "mb_artist_ids": set(), "mb_release_ids": set(), "source": "local",
    } for number, (title, artist) in enumerate([("Neon Memory", "Target Artist"), ("Midnight Echo", "Other Artist")], 1)]


@pytest.mark.parametrize("album_artist", [
    "Target Artist & Other Artist", "Target Artist x Other Artist", "Target Artist", "Target Artist; Other Artist",
])
def test_split_release_tagged_with_another_credit_spelling_is_found_locally(album_artist):
    metadata = split_release(
        credit(("Target Artist", ARTIST_ID), ("Other Artist", "other")), credit(("Target Artist", ARTIST_ID)),
    )
    scraper = scraper_for(raw_catalog([metadata]))

    scraper._audit_full_releases(inventory(album_artist))

    assert [(audit["found_count"], audit["missing_count"]) for audit in scraper.full_release_audits] == [(2, 0)]


def test_split_release_tagged_with_a_catalog_alias_is_found_locally():
    metadata = split_release(
        credit(("Machine Drum", ARTIST_ID), ("Other Artist", "other")), credit(("Machine Drum", ARTIST_ID)),
    )
    scraper = scraper_for(raw_catalog([metadata], aliases=["Machine Drum"]))
    scraper.catalog.name = "Machinedrum"

    scraper._audit_full_releases(inventory("Machinedrum"))

    assert [(audit["found_count"], audit["missing_count"]) for audit in scraper.full_release_audits] == [(2, 0)]


def test_split_release_tagged_with_an_unrelated_album_artist_is_not_counted():
    metadata = split_release(
        credit(("Target Artist", ARTIST_ID), ("Other Artist", "other")), credit(("Target Artist", ARTIST_ID)),
    )
    scraper = scraper_for(raw_catalog([metadata]))

    scraper._audit_full_releases(inventory("Somebody Else"))

    assert [(audit["found_count"], audit["missing_count"]) for audit in scraper.full_release_audits] == [(0, 2)]


def test_album_crediting_a_featured_artist_stays_an_ordinary_album():
    featured = credit(("Machine Drum", ARTIST_ID), ("Poly-Sci", "poly-sci"), joiner=" feat. ")
    metadata = release("Now You Know", ["Now You Know", "Sleep Patterns"], artist_credit=featured)
    for medium in metadata["medium-list"]:
        for track in medium["track-list"]:
            track["artist-credit"] = featured
    scraper = scraper_for(raw_catalog([metadata], aliases=["Machine Drum"]))

    scraper._audit_full_releases([])

    assert scraper.full_release_audits == []
    assert scraper._full_release_ids == set()


def test_featured_credit_without_artist_ids_stays_an_ordinary_album():
    metadata = release("Now You Know", ["Now You Know", "Sleep Patterns"])
    for medium in metadata["medium-list"]:
        for track in medium["track-list"]:
            track["artist-credit"] = [{"name": "Target Artist feat. Poly-Sci"}]
    scraper = scraper_for(raw_catalog([metadata]))

    scraper._audit_full_releases([])

    assert scraper.full_release_audits == []
