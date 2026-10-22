"""Tier 2 needs a folder that names the release as whole words, and artist evidence."""

import pytest

from discogseek.clients.musicbrainz import ArtistCatalog
from discogseek.core.text import normalize_text
from discogseek.services.reconciler import DiscographyReconciler


def lone_catalog(release_title="On", track_title="Ascension"):
    return ArtistCatalog({
        "artist": {"id": "lone", "name": "Lone"},
        "releases_artist": [{
            "id": "r1", "title": release_title, "release-group": {"id": "rg1", "primary-type": "EP"},
            "medium-list": [{"position": "1", "track-list": [
                {"id": "t1", "number": "1", "title": track_title,
                 "recording": {"id": "rec1", "title": track_title},
                 "artist-credit": [{"artist": {"id": "lone", "name": "Lone"}}]},
            ]}],
        }],
    })


def local(path, album, artists, title="Ascension", release_ids=()):
    return {
        "path": path, "filename": path.rsplit("/", 1)[-1], "title": title,
        "norm_title": normalize_text(title), "album": album, "norm_album": normalize_text(album),
        "track_number": "1", "artists": artists, "mb_track_ids": set(), "mb_rec_ids": set(),
        "mb_artist_ids": set(), "mb_release_ids": set(release_ids), "source": "local",
    }


def found(catalog, tracks):
    matches, _ = DiscographyReconciler(catalog, tracks).reconcile()
    return [(m["mb_track"]["title"], m["local_track"]["path"]) for m in matches]


def test_release_title_inside_another_folder_word_is_not_the_release():
    other = local("/music/Other Artist/Online Sessions/01 Ascension.flac", "Online Sessions", ["Other Artist"])

    assert found(lone_catalog(), [other]) == []


def test_genuine_release_folder_still_matches():
    genuine = local("/music/Lone/On/01 Ascension.flac", "On", ["Lone"])

    assert found(lone_catalog(), [genuine]) == [("Ascension", genuine["path"])]


@pytest.mark.parametrize("artists, release_ids, expected", [
    (["Other Artist"], (), False),
    (["Lone"], (), True),
    (["Other Artist"], ("r1",), True),
], ids=["other-artist", "artist-tag", "tagged-release"])
def test_folder_named_like_the_release_needs_artist_evidence(artists, release_ids, expected):
    # The album tag differs from the release, so only the folder names it.
    track = local("/music/Collections/Reflections/01 Ascension.flac", "Mixed Bag", artists,
                  release_ids=release_ids)

    assert bool(found(lone_catalog(release_title="Reflections"), [track])) is expected


@pytest.mark.parametrize("title, filename", [("Ascension", "01 Ascension.flac"), ("", "01.flac")],
                         ids=["titled", "numeric"])
def test_uncredited_download_in_a_folder_named_like_the_release_still_matches(title, filename):
    track = local(f"/music/downloads/Reflections/{filename}", "", [], title=title)
    track["filename"] = filename

    assert found(lone_catalog(release_title="Reflections"), [track]) == [("Ascension", track["path"])]
