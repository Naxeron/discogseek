"""Artist credit formatting and reconciliation of split releases."""

from discogseek.clients.musicbrainz import ArtistCatalog
from discogseek.core.text import normalize_text
from discogseek.services.reconciler import DiscographyReconciler
from tests.helpers.catalog import build_artist_catalog


class TestArtistCreditsAndSplitReleases:
    def test_complex_multi_artist_credit_formatting(self):
        """Preserves credited artist aliases and custom joinphrases."""
        ac_list = [
            {"name": "DJ Primary", "artist": {"id": "art-1", "name": "DJ Primary Official"}, "joinphrase": " feat. "},
            {"name": "MC Cool", "artist": {"id": "art-2", "name": "MC Cool"}, "joinphrase": " & "},
            {"name": "Singer Jane", "artist": {"id": "art-3", "name": "Jane Doe"}}
        ]
        credit = ArtistCatalog.format_credit(ac_list)
        assert credit == "DJ Primary feat. MC Cool & Singer Jane"

    def test_split_album_cross_contamination_prevented(self):
        """On a split release between Band A and Band B, reconciling Band A never matches Band B."""
        catalog_a = build_artist_catalog(
            artist_name="Band A",
            artist_id="band-a",
            releases=[{
                "title": "Split 7 Inch",
                "tracks": [
                    {
                        "title": "Side A Track",
                        "number": "1",
                        "artist_credit": [{"artist": {"id": "band-a", "name": "Band A"}}]
                    }
                ]
            }]
        )

        local_tracks = [
            {
                "path": "/music/Split 7 Inch/01 Side A Track.flac",
                "filename": "01 Side A Track.flac",
                "title": "Side A Track",
                "norm_title": normalize_text("Side A Track"),
                "album": "Split 7 Inch",
                "norm_album": normalize_text("Split 7 Inch"),
                "artists": ["Band A"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            {
                "path": "/music/Split 7 Inch/02 Side B Track.flac",
                "filename": "02 Side B Track.flac",
                "title": "Side B Track",
                "norm_title": normalize_text("Side B Track"),
                "album": "Split 7 Inch",
                "norm_album": normalize_text("Split 7 Inch"),
                "artists": ["Band B"],
                "track_number": "2",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
        ]

        rec = DiscographyReconciler(catalog_a, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert found[0]["mb_track"]["title"] == "Side A Track"
        assert len(missing) == 0
        assert not any(f["local_track"]["title"] == "Side B Track" for f in found)
