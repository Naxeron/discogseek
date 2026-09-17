"""Audit numeric filenames by disc and track number without duplicate bonus tracks."""

from unittest.mock import MagicMock

from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.library import LibraryReleaseService, _is_numeric_track_item
from discogseek.services.reconciler import is_purely_numeric_track


class TestNumericTrackAuditing:
    """Match numeric filenames to catalog tracks while preserving missing tracks."""

    def test_is_purely_numeric_track_helper(self):
        """Verify helper correctly classifies purely numeric files without false positives."""
        assert is_purely_numeric_track({"title": "", "filename": "01.flac"}) is True
        assert is_purely_numeric_track({"title": "01", "filename": "01.flac"}) is True
        assert is_purely_numeric_track({"title": "Track 01", "filename": "track 01.mp3"}) is True
        assert is_purely_numeric_track({"title": "Trk_02", "filename": "trk_02.flac"}) is True

        # False cases (must NOT be treated as purely numeric)
        assert is_purely_numeric_track({"title": "1999", "filename": "Prince - 1999.flac"}) is False
        assert is_purely_numeric_track({"title": "Song Title", "filename": "01 Song Title.flac"}) is False
        assert is_purely_numeric_track({"title": "One", "filename": "01 One.flac"}) is False

    def test_single_disc_numeric_filenames_audit(self):
        """
        Purely numeric filenames (01.flac, 02.flac, 03.flac) in a confirmed album folder
        match catalog tracks by track number without failing similarity or duplicating.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-album-num",
            "title": "Morning Glory",
            "artist-credit": [{"name": "Oasis"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Hello", "recording": {"id": "r1"}},
                        {"number": "2", "position": 2, "title": "Roll With It", "recording": {"id": "r2"}},
                        {"number": "3", "position": 3, "title": "Wonderwall", "recording": {"id": "r3"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Oasis",
            "title": "Morning Glory",
            "mb_release_id": "mb-album-num",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "01", "filename": "01.flac", "path": "/music/Oasis/01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "02", "filename": "02.flac", "path": "/music/Oasis/02.flac"},
                {"disc_number": 1, "track_number": "3", "track_num_int": 3, "title": "03", "filename": "03.flac", "path": "/music/Oasis/03.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "complete"
        assert audited["found_count"] == 3
        assert audited["missing_count"] == 0
        assert audited["completion_pct"] == 100.0

        # Zero duplicate bonus tracks
        assert len(audited["tracks"]) == 3
        titles = [t["title"] for t in audited["tracks"]]
        assert titles == ["Hello", "Roll With It", "Wonderwall"]

    def test_numeric_filenames_with_sequence_gap_in_audit(self):
        """
        Files are 01.flac and 03.flac (missing 02.flac).
        01.flac matches Track 1.
        03.flac matches Track 3.
        Track 2 is strictly identified as missing.
        03.flac MUST NOT match Track 2.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-gap-num",
            "title": "Album With Gap",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "First Song", "recording": {"id": "r1"}},
                        {"number": "2", "position": 2, "title": "Second Song", "recording": {"id": "r2"}},
                        {"number": "3", "position": 3, "title": "Third Song", "recording": {"id": "r3"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Artist",
            "title": "Album With Gap",
            "mb_release_id": "mb-gap-num",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "01.flac", "path": "/music/01.flac"},
                {"disc_number": 1, "track_number": "3", "track_num_int": 3, "title": "", "filename": "03.flac", "path": "/music/03.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 2
        assert audited["missing_count"] == 1

        t1 = next(t for t in audited["tracks"] if t["track_number"] == "1")
        t2 = next(t for t in audited["tracks"] if t["track_number"] == "2")
        t3 = next(t for t in audited["tracks"] if t["track_number"] == "3")

        assert t1["status"] == "found"
        assert t1["title"] == "First Song"
        assert t1["filename"] == "01.flac"

        assert t2["status"] == "missing"
        assert t2["title"] == "Second Song"
        assert t2["filename"] is None

        assert t3["status"] == "found"
        assert t3["title"] == "Third Song"
        assert t3["filename"] == "03.flac"

        # No duplicate bonus tracks
        assert len(audited["tracks"]) == 3

    def test_multi_disc_numeric_filenames_audit(self):
        """
        Multi-disc folder where both discs use numeric filenames:
        Disc 1 has 01.flac, 02.flac
        Disc 2 has 01.flac, 02.flac
        Ensure each disc matches only its own tracks without cross-disc collision.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-multi-num",
            "title": "Double Numeric Album",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Disc 1 Song 1", "recording": {"id": "d1r1"}},
                        {"number": "2", "position": 2, "title": "Disc 1 Song 2", "recording": {"id": "d1r2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Disc 2 Song 1", "recording": {"id": "d2r1"}},
                        {"number": "2", "position": 2, "title": "Disc 2 Song 2", "recording": {"id": "d2r2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Artist",
            "title": "Double Numeric Album",
            "mb_release_id": "mb-multi-num",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "01", "filename": "01.flac", "path": "/music/Disc 1/01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "02", "filename": "02.flac", "path": "/music/Disc 1/02.flac"},
                {"disc_number": 2, "track_number": "1", "track_num_int": 1, "title": "01", "filename": "01.flac", "path": "/music/Disc 2/01.flac"},
                {"disc_number": 2, "track_number": "2", "track_num_int": 2, "title": "02", "filename": "02.flac", "path": "/music/Disc 2/02.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "complete"
        assert audited["found_count"] == 4
        assert audited["missing_count"] == 0
        assert len(audited["tracks"]) == 4

        d1_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "1")
        d2_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "1")

        assert d1_t1["title"] == "Disc 1 Song 1"
        assert "/Disc 1/01.flac" in d1_t1["path"]

        assert d2_t1["title"] == "Disc 2 Song 1"
        assert "/Disc 2/01.flac" in d2_t1["path"]

    def test_multi_disc_numeric_cross_disc_rejection(self):
        """
        If Disc 1 has 01.flac, but Disc 2 has NO files, Disc 2 Track 1 must NOT match Disc 1 01.flac.
        Disc 2 Track 1 must remain missing.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-cross-reject",
            "title": "Cross Disc Test",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D1 Track 1", "recording": {"id": "r1"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D2 Track 1", "recording": {"id": "r2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Artist",
            "title": "Cross Disc Test",
            "mb_release_id": "mb-cross-reject",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "01", "filename": "01.flac", "path": "/music/Disc 1/01.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 1
        assert audited["missing_count"] == 1

        d1 = next(t for t in audited["tracks"] if t["disc_number"] == 1)
        d2 = next(t for t in audited["tracks"] if t["disc_number"] == 2)

        assert d1["status"] == "found"
        assert d1["title"] == "D1 Track 1"
        assert d2["status"] == "missing"
        assert d2["title"] == "D2 Track 1"

    def test_purely_numeric_multidisc_higher_discs_audit(self, tmp_path):
        """
        Verify flat folder purely numeric files on higher discs (3-01.flac, 3-02.flac, 4-01.flac)
        reconcile cleanly in audit_release without similarity penalty.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Electronic Artist" / "Catalog Numbers"
        album_dir.mkdir(parents=True)

        (album_dir / "3-01.flac").write_text("1")
        (album_dir / "3-03.flac").write_text("3")
        (album_dir / "4-01.flac").write_text("4")

        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-num-higher-discs",
            "title": "Catalog Numbers",
            "artist-credit": [{"name": "Electronic Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 3,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Modular Pulse", "recording": {"id": "r31"}},
                        {"number": "2", "position": 2, "title": "Resonant Filter", "recording": {"id": "r32"}},
                        {"number": "3", "position": 3, "title": "Sine Wave", "recording": {"id": "r33"}},
                    ]
                },
                {
                    "position": 4,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Sub Bass", "recording": {"id": "r41"}},
                    ]
                }
            ]
        }

        cache = UnifiedCacheManager(db_path=tmp_path / "num_higher.db")
        service = LibraryReleaseService(mb_client=mock_mb, cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        rel = releases[0]
        rel["mb_release_id"] = "mb-num-higher-discs"

        audited = service.audit_release(rel)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 3
        assert audited["missing_count"] == 1  # Disc 3 Track 2 is missing

        # Verify matched tracks
        d3_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 3 and t["track_number"] == "1")
        d3_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 3 and t["track_number"] == "2")
        d3_t3 = next(t for t in audited["tracks"] if t["disc_number"] == 3 and t["track_number"] == "3")
        d4_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 4 and t["track_number"] == "1")

        assert d3_t1["status"] == "found" and d3_t1["title"] == "Modular Pulse"
        assert d3_t2["status"] == "missing" and d3_t2["title"] == "Resonant Filter"
        assert d3_t3["status"] == "found" and d3_t3["title"] == "Sine Wave"
        assert d4_t1["status"] == "found" and d4_t1["title"] == "Sub Bass"

    def test_is_numeric_track_item_helper(self):
        """_is_numeric_track_item accurately differentiates numeric vs real titles."""
        # Pure numeric
        assert _is_numeric_track_item({"title": "", "filename": "01.flac"}) is True
        assert _is_numeric_track_item({"title": "01", "filename": "01.flac"}) is True
        assert _is_numeric_track_item({"title": "Track 01", "filename": "01.flac"}) is True

        # Compound numeric
        assert _is_numeric_track_item({"title": "", "filename": "1-01.flac"}) is True
        assert _is_numeric_track_item({"title": "1-01", "filename": "1-01.flac"}) is True
        assert _is_numeric_track_item({"title": "Track 01", "filename": "1-01.flac"}) is True
        assert _is_numeric_track_item({"title": "", "filename": "2-03.flac"}) is True
        assert _is_numeric_track_item({"title": "", "filename": "02.04.flac"}) is True
        assert _is_numeric_track_item({"title": "", "filename": "1_05.flac"}) is True

        # Non-numeric (has real title)
        assert _is_numeric_track_item({"title": "Paranoid Android", "filename": "1-01.flac"}) is False
        assert _is_numeric_track_item({"title": "Real Song", "filename": "01.flac"}) is False
        assert _is_numeric_track_item({"title": "1-01 Song Title", "filename": "1-01 Song Title.flac"}) is False

    def test_audit_release_compound_numeric_with_disc2_missing_track(self):
        """
        Audit a multi-disc release with compound numeric filenames:
        Official release:
          Disc 1: Track 1 ("Intro"), Track 2 ("Outro")
          Disc 2: Track 1 ("Part A"), Track 2 ("Part B")
        Local files:
          "1-01.flac" (D1 T1)
          "1-02.flac" (D1 T2)
          "2-02.flac" (D2 T2) - note 2-01 is MISSING!
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-comp-audit-gap",
            "title": "Dual Concept",
            "artist-credit": [{"name": "Electronic Project"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Intro", "recording": {"id": "rec-d1-1"}},
                        {"number": "2", "position": 2, "title": "Outro", "recording": {"id": "rec-d1-2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Part A", "recording": {"id": "rec-d2-1"}},
                        {"number": "2", "position": 2, "title": "Part B", "recording": {"id": "rec-d2-2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Electronic Project",
            "title": "Dual Concept",
            "mb_release_id": "mb-comp-audit-gap",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "1-01.flac", "path": "/music/1-01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "", "filename": "1-02.flac", "path": "/music/1-02.flac"},
                # Disc 2 track 2 only; track 1 is absent
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "", "filename": "2-02.flac", "path": "/music/2-02.flac"},
            ]
        }

        audited = service.audit_release(release_data)

        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 3
        assert audited["missing_count"] == 1
        assert len(audited["tracks"]) == 4

        # Verify Disc 2 Track 1 is flagged missing
        missing = [t for t in audited["tracks"] if t["status"] == "missing"]
        assert len(missing) == 1
        assert missing[0]["disc_number"] == 2
        assert missing[0]["track_number"] == "1"
        assert missing[0]["title"] == "Part A"

        # Verify Disc 2 Track 2 is matched to 2-02.flac
        d2_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "2")
        assert d2_t2["status"] == "found"
        assert d2_t2["filename"] == "2-02.flac"
        assert d2_t2["title"] == "Part B"

        # Verify Disc 1 tracks
        d1_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "1")
        assert d1_t1["status"] == "found"
        assert d1_t1["filename"] == "1-01.flac"
        assert d1_t1["title"] == "Intro"

        d1_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "2")
        assert d1_t2["status"] == "found"
        assert d1_t2["filename"] == "1-02.flac"
        assert d1_t2["title"] == "Outro"

    def test_audit_release_cross_disc_collision_numeric_prevention(self):
        """
        Ensure 2-01.flac NEVER falsely matches Disc 1 Track 1 even when track_number is '1'.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-collision-guard",
            "title": "Cross Guard",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D1 Song 1", "recording": {"id": "r1"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D2 Song 1", "recording": {"id": "r2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        # Only provide 2-01.flac (Disc 2 Track 1)
        release_data = {
            "artist": "Artist",
            "title": "Cross Guard",
            "mb_release_id": "mb-collision-guard",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "2-01.flac", "path": "/music/2-01.flac"},
            ]
        }

        audited = service.audit_release(release_data)

        # Disc 1 Track 1 must be MISSING
        d1_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "1")
        assert d1_t1["status"] == "missing", "2-01.flac erroneously matched Disc 1 Track 1!"

        # Disc 2 Track 1 must be FOUND
        d2_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "1")
        assert d2_t1["status"] == "found"
        assert d2_t1["filename"] == "2-01.flac"
        assert d2_t1["title"] == "D2 Song 1"

        assert audited["missing_count"] == 1
        assert audited["found_count"] == 1
        assert len(audited["tracks"]) == 2
