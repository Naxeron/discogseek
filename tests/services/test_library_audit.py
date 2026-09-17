"""MusicBrainz release reconciliation against library tracks."""

from unittest.mock import MagicMock

import pytest

from discogseek.services.library import LibraryReleaseService


def test_audit_release_reconciliation():
    """Tests auditing a release against MusicBrainz tracklist to classify found vs missing tracks."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # Mock MusicBrainz release response
    mb_mock.get_release_by_id.return_value = {
        "id": "rel-mbid-1234",
        "title": "Selected Ambient Works 85-92",
        "medium-list": [{
            "position": 1,
            "track-list": [
                {"position": 1, "number": "1", "title": "Xtal", "recording": {"id": "rec-1", "title": "Xtal"}},
                {"position": 2, "number": "2", "title": "Tha", "recording": {"id": "rec-2", "title": "Tha"}},
                {"position": 3, "number": "3", "title": "Pulsewidth", "recording": {"id": "rec-3", "title": "Pulsewidth"}},
                {"position": 4, "number": "4", "title": "Ageispolis", "recording": {"id": "rec-4", "title": "Ageispolis"}},
            ]
        }]
    }

    local_release = {
        "id": "test-rel-id",
        "title": "Selected Ambient Works 85-92",
        "artist": "Aphex Twin",
        "mb_release_id": "rel-mbid-1234",
        "tracks": [
            {
                "filename": "01 Xtal.flac",
                "title": "Xtal",
                "track_number": "1",
                "format": "FLAC",
                "bitrate": 900,
                "is_lossless": True,
                "quality_score": 90,
                "status": "found"
            },
            {
                "filename": "02 Tha.flac",
                "title": "Tha",
                "track_number": "2",
                "format": "FLAC",
                "bitrate": 850,
                "is_lossless": True,
                "quality_score": 90,
                "status": "found"
            }
        ]
    }

    audited = service.audit_release(local_release)
    assert audited["total_tracks_expected"] == 4
    assert audited["found_count"] == 2
    assert audited["missing_count"] == 2
    assert audited["completion_pct"] == 50.0
    assert audited["status"] == "has_missing"

    tracks = audited["tracks"]
    assert len(tracks) == 4
    assert tracks[0]["title"] == "Xtal" and tracks[0]["status"] == "found"
    assert tracks[1]["title"] == "Tha" and tracks[1]["status"] == "found"
    assert tracks[2]["title"] == "Pulsewidth" and tracks[2]["status"] == "missing"
    assert tracks[3]["title"] == "Ageispolis" and tracks[3]["status"] == "missing"

def test_audit_release_various_artists_reconciliation():
    """Tests auditing a release where local had 1 track by Exnoiz and MusicBrainz returns Various Artists with distinct track artists."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # Mock MusicBrainz response for Amen Destroyer (Various Artists compilation)
    mb_mock.search_release.return_value = [
        {"id": "amen-mbid-9999", "title": "Amen Destroyer", "artist-credit": [{"name": "Various Artists"}]}
    ]
    mb_mock.get_release_by_id.return_value = {
        "id": "amen-mbid-9999",
        "title": "Amen Destroyer",
        "artist-credit": [{"name": "Various Artists"}],
        "release-group": {"primary-type": "Compilation"},
        "medium-list": [{
            "position": 1,
            "track-list": [
                {
                    "position": 1, "number": "1", "title": "Destruction",
                    "artist-credit": [{"name": "Exnoiz"}],
                    "recording": {"id": "rec-exnoiz", "title": "Destruction"}
                },
                {
                    "position": 2, "number": "2", "title": "Amen Terror",
                    "artist-credit": [{"name": "Venetian Snares"}],
                    "recording": {"id": "rec-snares", "title": "Amen Terror"}
                },
                {
                    "position": 3, "number": "3", "title": "Mashup Core",
                    "artist-credit": [{"name": "Bong-Ra"}],
                    "recording": {"id": "rec-bongra", "title": "Mashup Core"}
                },
            ]
        }]
    }

    # Local release was mistakenly identified with artist="Exnoiz" because only 1 track was downloaded
    local_release = {
        "id": "local-amen-id",
        "title": "Amen Destroyer",
        "artist": "Exnoiz",
        "tracks": [
            {
                "filename": "01 - Exnoiz - Destruction.mp3",
                "title": "Destruction",
                "artist": "Exnoiz",
                "track_number": "1",
                "format": "MP3",
                "status": "found"
            }
        ]
    }

    audited = service.audit_release(local_release)
    assert audited["artist"] == "Various Artists"
    assert audited["is_va"] is True
    assert audited["found_count"] == 1
    assert audited["missing_count"] == 2
    assert audited["status"] == "has_missing"

    # Verify track artists: Track 1 is Exnoiz, Missing Track 2 is Venetian Snares, Missing Track 3 is Bong-Ra
    tracks = audited["tracks"]
    assert len(tracks) == 3
    assert tracks[0]["title"] == "Destruction" and tracks[0]["artist"] == "Exnoiz" and tracks[0]["status"] == "found"
    assert tracks[1]["title"] == "Amen Terror" and tracks[1]["artist"] == "Venetian Snares" and tracks[1]["status"] == "missing"
    assert tracks[2]["title"] == "Mashup Core" and tracks[2]["artist"] == "Bong-Ra" and tracks[2]["status"] == "missing"


@pytest.mark.parametrize("mb_artist, expected_artist, expected_va", [
    ("Buster Nalmi", "Buster Nalmi", False),
    (None, "Various Artists", True),
])
def test_audit_release_prefers_official_album_artist_over_inferred_compilation(
    mb_artist, expected_artist, expected_va,
):
    mb = MagicMock()
    mb.get_release_by_id.return_value = {
        "id": "lightning-mbid", "title": "Lightning",
        "artist-credit": [{"name": mb_artist}] if mb_artist else [],
        "medium-list": [{"position": 1, "track-list": [
            {"number": "1", "recording": {"id": "rec-1", "title": "Get High"}},
            {"number": "2", "artist-credit": [{"name": "The Busters"}],
             "recording": {"id": "rec-2", "title": "Band Edit"}},
        ]}],
    }
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    local = {
        "title": "Lightning", "artist": "Various Artists", "album_artist": "Various Artists",
        "is_va": True, "mb_release_id": "lightning-mbid", "tracks": [
            {"filename": "01 Get High.flac", "title": "Get High", "track_number": "1"},
        ],
    }

    audited = service.audit_release(local)

    assert audited["artist"] == audited["album_artist"] == expected_artist
    assert audited["is_va"] is expected_va
    assert audited["tracks"][0]["artist"] == expected_artist
    assert audited["tracks"][1]["artist"] == "The Busters"
    assert audited["found_count"] == audited["missing_count"] == 1
