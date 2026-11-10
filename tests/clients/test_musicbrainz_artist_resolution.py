"""Artist names that several MusicBrainz artists share resolve only when the query tells them apart."""

import json

import pytest

from discogseek.clients import musicbrainz
from discogseek.clients.musicbrainz import (
    AmbiguousArtistError,
    MusicBrainzClient,
    choose_artist,
    split_artist_hint,
)
from discogseek.services.soulseek import SlskdArtistScraper

ROCK = {"id": "c0842702-9ff3-4879-9e24-2c7b8fb91f9d", "name": "Repeater", "score": 100,
        "disambiguation": "Long Beach, CA rock band", "country": "US", "type": "Group"}
BREAKCORE = {"id": "b3386be0-0041-45dd-8c5c-33778eda7e29", "name": "Repeater", "score": 95,
             "disambiguation": "Breakcore artist"}
ACID = {"id": "0c1fb06f-55f4-4ee2-9cfd-7aeaf69a4a8d", "name": "Repeater", "score": 95,
        "disambiguation": "UK acid house duo", "country": "GB", "type": "Group",
        "tags": [{"count": 2, "name": "acid house"}]}
GAMMA = {"id": "ee2aa541-72db-4c79-9f61-e596b8d9ba53", "name": "Gamma Repeater", "score": 87}


class _Response:
    status_code = 200

    def __init__(self, artists):
        self._artists = artists

    def json(self):
        return {"artists": self._artists}


@pytest.fixture
def search(monkeypatch):
    """Answer MusicBrainz artist searches from memory and record each query."""
    queries = []
    results = {}

    def get(url, params=None, **kwargs):
        queries.append(params["query"])
        return _Response(results.get(params["query"], []))

    def unexpected(*args, **kwargs):
        pytest.fail("the JSON search answered; the musicbrainzngs fallback must not run")

    monkeypatch.setattr(musicbrainz.requests, "get", get)
    monkeypatch.setattr(musicbrainz.musicbrainzngs, "search_artists", unexpected)
    monkeypatch.setattr(musicbrainz.musicbrainzngs, "get_artist_by_id", unexpected)
    return queries, results


def _cached_choices(client):
    path = client.cache_dir / "artist_search_cache.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_shared_name_without_description_lists_every_namesake_instead_of_guessing(tmp_path, search):
    queries, results = search
    results["Repeater"] = [ROCK, BREAKCORE, ACID, GAMMA]
    client = MusicBrainzClient(cache_dir=tmp_path)

    with pytest.raises(AmbiguousArtistError) as raised:
        client.resolve_artist_mbid("Repeater")

    message = str(raised.value)
    assert message.splitlines()[0] == 'Several MusicBrainz artists are named "Repeater":'
    for artist in (ROCK, BREAKCORE, ACID):
        assert artist["id"] in message
    assert GAMMA["id"] not in message  # another name entirely
    assert "Repeater (Long Beach, CA rock band) [US, Group]" in message
    assert 'add words from its description in brackets, e.g. "Repeater (Long Beach, CA rock band)"' in message
    # Nothing was chosen, so nothing is remembered for the next run.
    assert "repeater" not in _cached_choices(client)


def test_bracketed_description_selects_the_namesake_and_is_remembered(tmp_path, search):
    queries, results = search
    results["Repeater (breakcore)"] = [BREAKCORE, ROCK, ACID]
    client = MusicBrainzClient(cache_dir=tmp_path)

    assert client.resolve_artist_mbid("Repeater (breakcore)") == (BREAKCORE["id"], "Repeater")
    assert queries == ["Repeater (breakcore)"]
    assert _cached_choices(client)["repeater (breakcore)"]["mbid"] == BREAKCORE["id"]

    # The remembered choice needs no further search.
    assert MusicBrainzClient(cache_dir=tmp_path).resolve_artist_mbid("Repeater (breakcore)")[0] == BREAKCORE["id"]
    assert queries == ["Repeater (breakcore)"]


@pytest.mark.parametrize("query, expected", [
    ("Repeater (Breakcore artist)", BREAKCORE),  # the disambiguation itself
    ("Repeater (GB)", ACID),                     # country
    ("Repeater (acid house)", ACID),             # a tag
    ("Repeater (us group)", ROCK),               # several description words together
])
def test_description_words_come_from_disambiguation_country_type_and_tags(expected, query):
    assert choose_artist(query, [ROCK, BREAKCORE, ACID]) is expected


def test_description_fitting_none_or_several_namesakes_is_still_ambiguous():
    with pytest.raises(AmbiguousArtistError) as none_fit:
        choose_artist("Repeater (jungle)", [ROCK, BREAKCORE])
    assert none_fit.value.candidates == [ROCK, BREAKCORE]
    assert str(none_fit.value).startswith('No MusicBrainz artist named "Repeater" matches "jungle".')

    with pytest.raises(AmbiguousArtistError) as several_fit:
        choose_artist("Repeater (group)", [ROCK, BREAKCORE, ACID])
    assert several_fit.value.candidates == [ROCK, ACID]
    assert str(several_fit.value).startswith('Several MusicBrainz artists named "Repeater" match "group":')


def test_the_only_artist_with_the_name_wins_over_a_better_scored_partial_name(tmp_path, search):
    queries, results = search
    results["Repeater"] = [GAMMA, BREAKCORE]
    assert MusicBrainzClient(cache_dir=tmp_path).resolve_artist_mbid("Repeater")[0] == BREAKCORE["id"]


def test_misspelled_name_keeps_musicbrainz_best_match(tmp_path, search):
    queries, results = search
    results["Repeatr"] = [BREAKCORE, ROCK]
    assert MusicBrainzClient(cache_dir=tmp_path).resolve_artist_mbid("Repeatr")[0] == BREAKCORE["id"]


def test_alias_counts_when_no_artist_bears_the_name_itself():
    kyou = {"id": "kyou", "name": "kyou1110", "aliases": [{"name": "AmazingAnguillaJaponica"}]}
    other = {"id": "other", "name": "Anguilla"}
    assert choose_artist("AmazingAnguillaJaponica", [other, kyou]) is kyou
    # A name beats an alias: another artist's alias does not make a name ambiguous.
    namesake = {"id": "namesake", "name": "AmazingAnguillaJaponica"}
    assert choose_artist("AmazingAnguillaJaponica", [kyou, namesake]) is namesake


def test_a_name_that_itself_ends_in_brackets_is_not_read_as_a_description():
    bracketed = {"id": "bracketed", "name": "Foo (Bar)"}
    plain = {"id": "plain", "name": "Foo"}
    assert choose_artist("Foo (Bar)", [plain, bracketed]) is bracketed
    assert split_artist_hint("Foo (Bar)") == ("Foo", "Bar")
    assert split_artist_hint("Sunn O)))") == ("Sunn O)))", "")
    assert split_artist_hint("(hed) p.e.") == ("(hed) p.e.", "")
    assert split_artist_hint("Foo ()") == ("Foo ()", "")


def _cache_artist(client, artist):
    (client.cache_dir / f"artist_{artist['id']}.json").write_text(
        json.dumps({"artist": artist}), encoding="utf-8",
    )


def test_a_fetched_artist_answers_its_name_only_while_no_other_fetched_artist_shares_it(tmp_path, search):
    queries, results = search
    client = MusicBrainzClient(cache_dir=tmp_path)
    _cache_artist(client, {**BREAKCORE, "alias-list": [], "tag-list": [{"count": "1", "name": "breakcore"}]})

    assert client.resolve_artist_mbid("Repeater") == (BREAKCORE["id"], "Repeater")
    assert client.resolve_artist_mbid("REPEATER") == (BREAKCORE["id"], "Repeater")
    assert queries == []
    # A fetched artist's answer is never remembered as the name's meaning ...
    assert "repeater" not in _cached_choices(client)

    # ... so fetching a namesake later makes the name ambiguous again.
    _cache_artist(client, ROCK)
    results["Repeater"] = [ROCK, BREAKCORE]
    with pytest.raises(AmbiguousArtistError):
        client.resolve_artist_mbid("Repeater")
    assert queries == ["Repeater"]


def test_a_fetched_artists_alias_is_not_its_name(tmp_path, search):
    # Squarepusher's alias 'Stereotype' is also the name of other artists.
    queries, results = search
    client = MusicBrainzClient(cache_dir=tmp_path)
    squarepusher = {"id": "sqp", "name": "Squarepusher", "alias-list": [{"alias": "Stereotype"}]}
    _cache_artist(client, squarepusher)
    results["Stereotype"] = [{"id": "st-1", "name": "Stereotype"}, {"id": "st-2", "name": "Stereotype"},
                             {**squarepusher, "aliases": [{"name": "Stereotype"}]}]

    with pytest.raises(AmbiguousArtistError):
        client.resolve_artist_mbid("Stereotype")
    assert queries == ["Stereotype"]
    assert "stereotype" not in _cached_choices(client)


def test_an_alias_still_resolves_through_the_search_when_nobody_bears_it_as_a_name(tmp_path, search):
    queries, results = search
    client = MusicBrainzClient(cache_dir=tmp_path)
    kyou = {"id": "kyou", "name": "kyou1110", "aliases": [{"name": "AmazingAnguillaJaponica"}]}
    _cache_artist(client, {**kyou, "alias-list": [{"alias": "AmazingAnguillaJaponica"}]})
    results["AmazingAnguillaJaponica"] = [kyou]

    assert client.resolve_artist_mbid("AmazingAnguillaJaponica") == ("kyou", "kyou1110")
    assert queries == ["AmazingAnguillaJaponica"]


def test_choices_remembered_by_the_old_top_hit_logic_are_not_trusted(tmp_path, search):
    queries, results = search
    client = MusicBrainzClient(cache_dir=tmp_path)
    (tmp_path / "artist_search_cache.json").write_text(json.dumps({
        "repeater": [ROCK["id"], "Repeater", " (Long Beach, CA rock band)", " [US]"],
    }), encoding="utf-8")
    results["Repeater"] = [ROCK, BREAKCORE]

    with pytest.raises(AmbiguousArtistError):
        client.resolve_artist_mbid("Repeater")
    assert queries == ["Repeater"]


def test_a_description_without_an_artist_of_that_name_never_takes_the_top_hit(tmp_path, search):
    # 'Repeatr (breakcore)': the description's word finds the artist 'Breakcore'.
    queries, results = search
    results["Repeatr (breakcore)"] = [{"id": "bc", "name": "Breakcore", "score": 100}, BREAKCORE]

    with pytest.raises(ValueError, match='No MusicBrainz artist is named "Repeatr"'):
        MusicBrainzClient(cache_dir=tmp_path).resolve_artist_mbid("Repeatr (breakcore)")


def test_fullwidth_and_nested_brackets_hold_descriptions():
    assert split_artist_hint("Repeater（breakcore）") == ("Repeater", "breakcore")
    assert choose_artist("Repeater（breakcore）", [ROCK, BREAKCORE]) is BREAKCORE
    stewart = {"id": "js", "name": "John Stewart", "disambiguation": "singer-songwriter (Kingston Trio)"}
    other = {"id": "js2", "name": "John Stewart", "disambiguation": "British racing driver"}
    assert split_artist_hint("John Stewart (singer-songwriter (Kingston Trio))") == (
        "John Stewart", "singer-songwriter (Kingston Trio)")
    assert choose_artist("John Stewart (singer-songwriter (Kingston Trio))", [other, stewart]) is stewart
    # The advice names a description the query parser reads back.
    with pytest.raises(AmbiguousArtistError) as raised:
        choose_artist("John Stewart", [stewart, other])
    assert 'e.g. "John Stewart (singer-songwriter Kingston Trio)"' in str(raised.value)
    assert choose_artist("John Stewart (singer-songwriter Kingston Trio)", [other, stewart]) is stewart


def test_names_that_only_transliterate_alike_are_different_names(tmp_path, search):
    # '李剑' and '李健' both fold to 'li jian'; a fetched '李健' must not answer '李剑'.
    queries, results = search
    client = MusicBrainzClient(cache_dir=tmp_path)
    _cache_artist(client, {"id": "lj", "name": "李健"})
    results["李剑"] = [{"id": "lj2", "name": "李剑"}, {"id": "lj", "name": "李健"}]

    assert client.resolve_artist_mbid("李剑") == ("lj2", "李剑")
    assert queries == ["李剑"]


def test_soulseek_searches_use_the_name_without_its_description():
    scraper = SlskdArtistScraper("Repeater (breakcore)", slskd_client=object())
    assert scraper.search_artist == "Repeater"
    assert SlskdArtistScraper("b3386be0-0041-45dd-8c5c-33778eda7e29", slskd_client=object()).search_artist is None
