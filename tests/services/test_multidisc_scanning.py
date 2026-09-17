"""Multi-disc library scanning, disc-track parsing, and independent sequence gaps."""

import pytest

from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.library import LibraryReleaseService, parse_disc_and_track_number


class TestMultiDiscFolders:
    """Group nested and flat multi-disc folders into a single release."""

    def test_nested_disc1_disc2_grouping(self, tmp_path):
        """Nested Disc 1 / Disc 2 folders group into a single release with correct disc numbers."""
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Radiohead" / "OK Computer OKNOTOK 1997 2017"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        (d1 / "01 Airbag.flac").write_text("audio1")
        (d1 / "02 Paranoid Android.flac").write_text("audio2")
        (d2 / "01 I Promise.flac").write_text("audio3")
        (d2 / "02 Man of War.flac").write_text("audio4")

        cache = UnifiedCacheManager(db_path=tmp_path / "test_nested_disc.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1, f"Expected 1 grouped release, got {len(releases)}"

        rel = releases[0]
        assert rel["title"] == "OK Computer OKNOTOK 1997 2017"
        assert rel["artist"] == "Radiohead"

        tracks = rel["tracks"]
        assert len(tracks) == 4

        d1_tracks = [t for t in tracks if t["disc_number"] == 1]
        d2_tracks = [t for t in tracks if t["disc_number"] == 2]
        assert len(d1_tracks) == 2
        assert len(d2_tracks) == 2
        assert [t["track_num_int"] for t in d1_tracks] == [1, 2]
        assert [t["track_num_int"] for t in d2_tracks] == [1, 2]

    def test_nested_cd01_cd02_grouping(self, tmp_path):
        """Zero-padded CD01 / CD02 subfolders group correctly into single release."""
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Daft Punk" / "Alive 2007"
        cd1 = album_dir / "CD01"
        cd2 = album_dir / "CD02"
        cd1.mkdir(parents=True)
        cd2.mkdir(parents=True)

        (cd1 / "01 Robot Rock.flac").write_text("audio1")
        (cd2 / "01 One More Time.flac").write_text("audio2")

        cache = UnifiedCacheManager(db_path=tmp_path / "test_cd_pad.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]
        assert len(rel["tracks"]) == 2

        t1 = next(t for t in rel["tracks"] if t["filename"] == "01 Robot Rock.flac")
        t2 = next(t for t in rel["tracks"] if t["filename"] == "01 One More Time.flac")
        assert t1["disc_number"] == 1
        assert t2["disc_number"] == 2

    def test_side_a_side_b_grouping(self, tmp_path):
        """'Side A' and 'Side B' subfolders map to disc 1 and disc 2."""
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Pink Floyd" / "The Dark Side of the Moon"
        side_a = album_dir / "Side A"
        side_b = album_dir / "Side B"
        side_a.mkdir(parents=True)
        side_b.mkdir(parents=True)

        (side_a / "01 Speak to Me.flac").write_text("audio1")
        (side_b / "01 Money.flac").write_text("audio2")

        cache = UnifiedCacheManager(db_path=tmp_path / "test_vinyl_sides.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        t_a = next(t for t in rel["tracks"] if "Speak to Me" in t["title"])
        t_b = next(t for t in rel["tracks"] if "Money" in t["title"])
        assert t_a["disc_number"] == 1
        assert t_b["disc_number"] == 2

    def test_flat_folder_disc_prefixes_preserve_grouping_without_false_gaps(self, tmp_path):
        """Disc-track prefixes distinguish discs in a flat album directory.

        Two tracks per disc must remain on their respective discs, without
        creating false sequence gaps from the repeated track numbers."""
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Flat Double Album"
        album_dir.mkdir(parents=True)

        (album_dir / "1-01 Song A.flac").write_text("a")
        (album_dir / "1-02 Song B.flac").write_text("b")
        (album_dir / "2-01 Song C.flac").write_text("c")
        (album_dir / "2-02 Song D.flac").write_text("d")

        cache = UnifiedCacheManager(db_path=tmp_path / "flat_probe.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        d1 = [t for t in rel["tracks"] if t.get("disc_number") == 1 and t.get("status") == "found"]
        d2 = [t for t in rel["tracks"] if t.get("disc_number") == 2 and t.get("status") == "found"]

        # Expected: 2 tracks on Disc 1, 2 tracks on Disc 2, 0 missing tracks
        assert len(d2) == 2, f"CRITICAL BUG: Expected 2 tracks on Disc 2, but got {len(d2)}. Disc 2 was collapsed into Disc 1!"
        assert len(d1) == 2, f"CRITICAL BUG: Expected 2 tracks on Disc 1, but got {len(d1)}"
        assert rel["missing_count"] == 0, f"CRITICAL BUG: Expected 0 missing tracks, but gap analysis generated {rel['missing_count']} false missing tracks!"

    def test_3_disc_flat_folder_multi_gap_detection(self, tmp_path):
        """
        3-disc flat album in a single folder:
        Disc 1: 1-01, 1-02 (complete)
        Disc 2: 2-01, 2-03 (track 2 missing!)
        Disc 3: 3-02, 3-03 (track 1 missing!)
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Prog Artist" / "Triple Odyssey"
        album_dir.mkdir(parents=True)

        (album_dir / "1-01 Act I Part 1.flac").write_text("a")
        (album_dir / "1-02 Act I Part 2.flac").write_text("b")
        (album_dir / "2-01 Act II Part 1.flac").write_text("c")
        # 2-02 is MISSING
        (album_dir / "2-03 Act II Part 3.flac").write_text("d")
        # 3-01 is MISSING
        (album_dir / "3-02 Act III Part 2.flac").write_text("e")
        (album_dir / "3-03 Act III Part 3.flac").write_text("f")

        cache = UnifiedCacheManager(db_path=tmp_path / "triple.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["title"] == "Triple Odyssey"
        assert rel["status"] == "has_missing"
        assert rel["found_count"] == 6
        assert rel["missing_count"] == 2
        assert rel["total_tracks_expected"] == 8

        missing_tracks = [t for t in rel["tracks"] if t["status"] == "missing"]
        assert len(missing_tracks) == 2

        d2_m = [t for t in missing_tracks if t["disc_number"] == 2]
        d3_m = [t for t in missing_tracks if t["disc_number"] == 3]

        assert len(d2_m) == 1
        assert d2_m[0]["track_num_int"] == 2
        assert d2_m[0]["title"] == "Disc 2 Track 02 (Missing)"

        assert len(d3_m) == 1
        assert d3_m[0]["track_num_int"] == 1
        assert d3_m[0]["title"] == "Disc 3 Track 01 (Missing)"

        # Verify ordering: sorted by (disc_number, track_num_int)
        seq = [(t["disc_number"], t["track_num_int"], t["status"]) for t in rel["tracks"]]
        assert seq == [
            (1, 1, "found"),
            (1, 2, "found"),
            (2, 1, "found"),
            (2, 2, "missing"),
            (2, 3, "found"),
            (3, 1, "missing"),
            (3, 2, "found"),
            (3, 3, "found"),
        ]

    def test_vinyl_4_sides_flat_folder_alternating_gaps(self, tmp_path):
        """
        4 vinyl sides in flat folder with gaps on alternating sides:
        Side A (D1): A1, A3 (A2 missing)
        Side B (D2): B2 (B1 missing)
        Side C (D3): C1, C2 (complete)
        Side D (D4): D1 (complete single track side)
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Electronic" / "Quad Vinyl EP"
        album_dir.mkdir(parents=True)

        (album_dir / "A1 Synth Wave.flac").write_text("1")
        (album_dir / "A3 Neon Drive.flac").write_text("3")
        (album_dir / "B2 Cyber City.flac").write_text("4")
        (album_dir / "C1 Retro Sunset.flac").write_text("5")
        (album_dir / "C2 Night Grid.flac").write_text("6")
        (album_dir / "D1 Outro Horizon.flac").write_text("7")

        cache = UnifiedCacheManager(db_path=tmp_path / "quad_vinyl.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["status"] == "has_missing"
        assert rel["found_count"] == 6
        assert rel["missing_count"] == 2
        assert rel["total_tracks_expected"] == 8

        d1_missing = [t for t in rel["tracks"] if t["disc_number"] == 1 and t["status"] == "missing"]
        assert len(d1_missing) == 1
        assert d1_missing[0]["track_num_int"] == 2

        d2_missing = [t for t in rel["tracks"] if t["disc_number"] == 2 and t["status"] == "missing"]
        assert len(d2_missing) == 1
        assert d2_missing[0]["track_num_int"] == 1

        d3_missing = [t for t in rel["tracks"] if t["disc_number"] == 3 and t["status"] == "missing"]
        assert len(d3_missing) == 0

        d4_missing = [t for t in rel["tracks"] if t["disc_number"] == 4 and t["status"] == "missing"]
        assert len(d4_missing) == 0


class TestMultiDiscSequenceGaps:
    """Detect sequence gaps independently on each disc."""

    def test_disc1_missing_track_never_obscured_by_disc2_track(self, tmp_path):
        """
        Disc 1 is missing track 2 (has tracks 1, 3).
        Disc 2 HAS track 2 (has tracks 1, 2, 3).
        Ensure Disc 1 Track 2 is strictly identified as missing and NOT obscured.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Nine Inch Nails" / "The Fragile"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        # Disc 1: tracks 1, 3
        (d1 / "01 Track One.flac").write_text("data1")
        (d1 / "03 Track Three.flac").write_text("data3")

        # Disc 2: tracks 1, 2, 3
        (d2 / "01 Track One.flac").write_text("data4")
        (d2 / "02 Track Two.flac").write_text("data5")
        (d2 / "03 Track Three.flac").write_text("data6")

        cache = UnifiedCacheManager(db_path=tmp_path / "gap_d1_d2.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["status"] == "has_missing"
        assert rel["missing_count"] == 1
        assert rel["found_count"] == 5

        # Inspect missing tracks specifically
        missing = [t for t in rel["tracks"] if t["status"] == "missing"]
        assert len(missing) == 1
        m_trk = missing[0]
        assert m_trk["disc_number"] == 1
        assert m_trk["track_num_int"] == 2
        assert "Disc 1 Track 02 (Missing)" in m_trk["title"]

        # Ensure Disc 2 Track 2 is present and found
        d2_t2 = next(t for t in rel["tracks"] if t.get("disc_number") == 2 and t.get("track_num_int") == 2)
        assert d2_t2["status"] == "found"

    def test_disc2_missing_track_never_obscured_by_disc1_track(self, tmp_path):
        """
        Disc 1 has tracks 1, 2, 3 (complete).
        Disc 2 is missing track 2 (has tracks 1, 3).
        Ensure Disc 2 Track 2 is strictly identified as missing.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Two Disc Album"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        (d1 / "01 Song 1.flac").write_text("data1")
        (d1 / "02 Song 2.flac").write_text("data2")
        (d1 / "03 Song 3.flac").write_text("data3")

        (d2 / "01 Song 4.flac").write_text("data4")
        (d2 / "03 Song 6.flac").write_text("data6")

        cache = UnifiedCacheManager(db_path=tmp_path / "gap_d2_missing.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        missing = [t for t in rel["tracks"] if t["status"] == "missing"]
        assert len(missing) == 1
        assert missing[0]["disc_number"] == 2
        assert missing[0]["track_num_int"] == 2
        assert "Disc 2 Track 02 (Missing)" in missing[0]["title"]

    def test_3disc_simultaneous_independent_gaps(self, tmp_path):
        """
        3-disc set with different gaps on each disc:
        Disc 1 missing track 2
        Disc 2 missing track 1
        Disc 3 missing track 3
        All 3 gaps must be independently and accurately identified.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Triple Box Set"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d3 = album_dir / "Disc 3"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        d3.mkdir(parents=True)

        # Disc 1: 1, 3 (missing 2)
        (d1 / "01 D1T1.flac").write_text("1")
        (d1 / "03 D1T3.flac").write_text("3")

        # Disc 2: 2, 3 (missing 1)
        (d2 / "02 D2T2.flac").write_text("2")
        (d2 / "03 D2T3.flac").write_text("3")

        # Disc 3: 1, 2, 4 (missing 3)
        (d3 / "01 D3T1.flac").write_text("1")
        (d3 / "02 D3T2.flac").write_text("2")
        (d3 / "04 D3T4.flac").write_text("4")

        cache = UnifiedCacheManager(db_path=tmp_path / "triple_box.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        missing = [t for t in rel["tracks"] if t["status"] == "missing"]
        assert len(missing) == 3

        missing_tuples = [(t["disc_number"], t["track_num_int"]) for t in missing]
        assert (1, 2) in missing_tuples
        assert (2, 1) in missing_tuples
        assert (3, 3) in missing_tuples

    def test_4disc_nested_gap_tracking_in_scan(self, tmp_path):
        """
        Verify 4-disc album folder hierarchy correctly tracks gaps independently on each disc:
        - Disc 1: tracks 1, 2, 3 (complete)
        - Disc 2: tracks 1, 3 (track 2 missing)
        - Disc 3: tracks 2, 4 (tracks 1 and 3 missing)
        - Disc 4: tracks 2, 3 (track 1 missing)
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "The Clash" / "Sandinista"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d3 = album_dir / "Disc 3"
        d4 = album_dir / "Disc 4"
        for d in (d1, d2, d3, d4):
            d.mkdir(parents=True)

        (d1 / "01 Track 1-1.flac").write_text("1")
        (d1 / "02 Track 1-2.flac").write_text("2")
        (d1 / "03 Track 1-3.flac").write_text("3")

        (d2 / "01 Track 2-1.flac").write_text("4")
        (d2 / "03 Track 2-3.flac").write_text("5")

        (d3 / "02 Track 3-2.flac").write_text("6")
        (d3 / "04 Track 3-4.flac").write_text("7")

        (d4 / "02 Track 4-2.flac").write_text("8")
        (d4 / "03 Track 4-3.flac").write_text("9")

        cache = UnifiedCacheManager(db_path=tmp_path / "clash.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["status"] == "has_missing"
        assert rel["found_count"] == 9
        assert rel["missing_count"] == 4  # D2T2, D3T1, D3T3, D4T1

        # Check Disc 1
        d1_tracks = [t for t in rel["tracks"] if t["disc_number"] == 1]
        assert len(d1_tracks) == 3
        assert all(t["status"] == "found" for t in d1_tracks)

        # Check Disc 2
        d2_missing = [t for t in rel["tracks"] if t["disc_number"] == 2 and t["status"] == "missing"]
        assert len(d2_missing) == 1
        assert d2_missing[0]["track_number"] == "2"
        assert d2_missing[0]["title"] == "Disc 2 Track 02 (Missing)"

        # Check Disc 3
        d3_missing = [t for t in rel["tracks"] if t["disc_number"] == 3 and t["status"] == "missing"]
        assert len(d3_missing) == 2
        assert {t["track_number"] for t in d3_missing} == {"1", "3"}
        assert {t["title"] for t in d3_missing} == {"Disc 3 Track 01 (Missing)", "Disc 3 Track 03 (Missing)"}

        # Check Disc 4
        d4_missing = [t for t in rel["tracks"] if t["disc_number"] == 4 and t["status"] == "missing"]
        assert len(d4_missing) == 1
        assert d4_missing[0]["track_number"] == "1"
        assert d4_missing[0]["title"] == "Disc 4 Track 01 (Missing)"

    def test_3_disc_boxset_asymmetric_gaps(self, tmp_path):
        """
        A 3-disc box set where:
        - Disc 1 has tracks 1, 3, 5 present -> gaps 2, 4
        - Disc 2 has tracks 1, 2, 3 present -> complete (0 gaps)
        - Disc 3 has track 4 present only   -> gaps 1, 2, 3
        Total 7 found tracks, 5 missing tracks across discs.
        Must track gaps by (disc_number, track_number) without cross-disc collision.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Pink Floyd" / "The Early Years Box"
        album_dir.mkdir(parents=True)

        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d3 = album_dir / "Disc 3"
        d1.mkdir()
        d2.mkdir()
        d3.mkdir()

        # Disc 1
        (d1 / "01 Arnold Layne.flac").write_text("dummy")
        (d1 / "03 See Emily Play.flac").write_text("dummy")
        (d1 / "05 Matilda Mother.flac").write_text("dummy")

        # Disc 2 (complete)
        (d2 / "01 Astronomy Domine.flac").write_text("dummy")
        (d2 / "02 Lucifer Sam.flac").write_text("dummy")
        (d2 / "03 Interstellar Overdrive.flac").write_text("dummy")

        # Disc 3 (only track 4)
        (d3 / "04 Set the Controls.flac").write_text("dummy")

        cache = UnifiedCacheManager(db_path=tmp_path / "boxset.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["status"] == "has_missing"
        assert rel["found_count"] == 7
        assert rel["missing_count"] == 5  # Disc 1 (2, 4) + Disc 3 (1, 2, 3) = 5 missing

        # Verify exact missing tracks
        missing = [t for t in rel["tracks"] if t["status"] == "missing"]
        missing_tuples = {(t["disc_number"], t["track_num_int"]) for t in missing}
        expected_missing = {(1, 2), (1, 4), (3, 1), (3, 2), (3, 3)}
        assert missing_tuples == expected_missing


class TestDiscTrackNumberParsing:
    """Parse disc and track numbers from tags and filenames."""

    def test_disc_prefix_overrides_default_metadata_disc(self):
        """Disc prefixes must override the default metadata disc number of 1.

        AudioMetadata defaults disc_number to 1. A tag such as "2-01" still
        identifies disc 2 when that default is passed to the parser."""
        # When meta_disc is None, it extracts disc 2:
        d_clean, t_clean, _ = parse_disc_and_track_number("2-01", meta_disc=None)
        assert d_clean == 2 and t_clean == 1, "Expected disc=2, track=1 when meta_disc is None"

        # scan_library_releases passes the default metadata disc number of 1:
        d_actual, t_actual, _ = parse_disc_and_track_number("2-01", meta_disc=1)
        # The explicit '2-01' prefix must still identify disc 2:
        assert d_actual == 2, f"CRITICAL BUG: Expected disc=2 from '2-01', but got {d_actual} because meta_disc=1 suppressed it!"

    def test_flat_folder_higher_disc_prefixes(self):
        """Verify parse_disc_and_track_number correctly handles higher discs (3 through 8)."""
        cases = [
            ("3-01 Song.flac", 3, 1),
            ("03-02 Song.flac", 3, 2),
            ("3.04 Song.flac", 3, 4),
            ("4-01 Song.flac", 4, 1),
            ("04-05 Song.flac", 4, 5),
            ("Side C 01 Song.flac", 3, 1),
            ("Side D 02 Song.flac", 4, 2),
            ("Side E 03 Song.flac", 5, 3),
            ("Side F 01 Song.flac", 6, 1),
            ("Side G 02 Song.flac", 7, 2),
            ("Side H 01 Song.flac", 8, 1),
            ("C1 Song.flac", 3, 1),
            ("D2 Song.flac", 4, 2),
            ("E03 Song.flac", 5, 3),
            ("Disc 3 - 01 Song.flac", 3, 1),
            ("CD 4 - 02 Song.flac", 4, 2),
        ]
        for fn, exp_d, exp_t in cases:
            # Without meta_disc
            d, t, _ = parse_disc_and_track_number(None, filename=fn, meta_disc=None)
            assert d == exp_d and t == exp_t, f"{fn} -> disc={d}, trk={t}, expected disc={exp_d}, trk={exp_t}"
            # With default meta_disc=1 (as passed from scan_library_releases)
            d1, t1, _ = parse_disc_and_track_number(None, filename=fn, meta_disc=1)
            assert d1 == exp_d and t1 == exp_t, f"{fn} with meta_disc=1 -> disc={d1}, trk={t1}, expected disc={exp_d}, trk={exp_t}"

    @pytest.mark.parametrize("raw_track,filename,meta_disc,exp_disc,exp_trk,exp_total", [
        # Standard track numbering
        ("1", "01 Song.flac", None, 1, 1, None),
        ("01", "01 Song.flac", None, 1, 1, None),
        ("1/12", "01 Song.flac", None, 1, 1, 12),
        ("05/20", "05 Song.flac", 1, 1, 5, 20),

        # Multi-disc tag formats
        ("1-01", "Song.flac", None, 1, 1, None),
        ("2-04", "Song.flac", None, 2, 4, None),
        ("03-01", "Song.flac", None, 3, 1, None),
        ("1.01", "Song.flac", None, 1, 1, None),
        ("2.05", "Song.flac", None, 2, 5, None),

        # Vinyl side notation in tag
        ("A1", "Song.flac", None, 1, 1, None),
        ("a1", "Song.flac", None, 1, 1, None),
        ("B1", "Song.flac", None, 2, 1, None),
        ("B02", "Song.flac", None, 2, 2, None),
        ("C3", "Song.flac", None, 3, 3, None),
        ("D12", "Song.flac", None, 4, 12, None),
        ("Side A", "Song.flac", None, 1, 1, None),
        ("Side B", "Song.flac", None, 2, 1, None),
        ("Side B 03", "Song.flac", None, 2, 3, None),
        ("Vinyl Side C", "Song.flac", None, 3, 1, None),
        ("Side 2 - 01", "Song.flac", None, 2, 1, None),
        ("Disc 2 - 01", "Song.flac", None, 2, 1, None),
        ("CD 3 - 04", "Song.flac", None, 3, 4, None),

        # Filename prefix when tag has plain digits or is None
        ("1", "1-01 Track.flac", None, 1, 1, None),
        ("1", "2-01 Track.flac", None, 2, 1, None),
        ("2", "2-02 Track.flac", 1, 2, 2, None),
        (None, "1-01 Track.flac", None, 1, 1, None),
        (None, "2-03 Track.flac", None, 2, 3, None),
        (None, "03-05 Track.flac", None, 3, 5, None),
        (None, "1.01 Track.flac", None, 1, 1, None),
        (None, "2.04 Track.flac", None, 2, 4, None),
        (None, "A1 Track.flac", None, 1, 1, None),
        (None, "B2 Track.flac", None, 2, 2, None),
        (None, "C03 Track.flac", None, 3, 3, None),
        (None, "Side A 01 - Track.flac", None, 1, 1, None),
        (None, "Side B 02 - Track.flac", None, 2, 2, None),
        (None, "Side 2 - 01 Track.flac", None, 2, 1, None),
        (None, "Disc 2 - 03 Track.flac", None, 2, 3, None),
        (None, "CD 2 - 01 Track.flac", None, 2, 1, None),
        (None, "CD 03 - 04 Track.flac", None, 3, 4, None),

        # Purely numeric files
        (None, "01.flac", None, 1, 1, None),
        (None, "02.mp3", None, 1, 2, None),
        (None, "1-01.flac", None, 1, 1, None),
        (None, "2-01.flac", None, 2, 1, None),
        (None, "2-02.flac", None, 2, 2, None),
        (None, "A1.flac", None, 1, 1, None),
        (None, "B1.flac", None, 2, 1, None),

        # Meta disc precedence when not default 1
        ("1", "01 Song.flac", 2, 2, 1, None),
        ("2", "02 Song.flac", 3, 3, 2, None),
    ])
    def test_parse_disc_and_track_matrix(
        self, raw_track, filename, meta_disc, exp_disc, exp_trk, exp_total
    ):
        d, t, tot = parse_disc_and_track_number(raw_track, filename=filename, meta_disc=meta_disc)
        assert d == exp_disc, f"Disc mismatch for ({raw_track}, {filename}, {meta_disc}): expected {exp_disc}, got {d}"
        assert t == exp_trk, f"Track mismatch for ({raw_track}, {filename}, {meta_disc}): expected {exp_trk}, got {t}"
        if exp_total is not None:
            assert tot == exp_total, f"Total mismatch: expected {exp_total}, got {tot}"

    def test_malformed_and_boundary_disc_inputs(self):
        """Check behavior on malformed and boundary inputs."""
        # Empty string
        d, t, _ = parse_disc_and_track_number("", filename="")
        assert d == 1
        assert t is None

        # None inputs
        d, t, _ = parse_disc_and_track_number(None, filename=None)
        assert d == 1
        assert t is None

        # Disc number outside 1..20 boundary returns None or 1, and effective_disc resolves to 1
        d, t, _ = parse_disc_and_track_number("99-01", filename=None)
        assert d is None or d == 1
        assert t == 1

    def test_flat_folder_disc_track_prefixes(self):
        """
        Test parse_disc_and_track_number across varied real-world disc/track prefix formats:
        - '1-01 Song.flac' -> Disc 1, Track 1
        - '2-05 Song.flac' -> Disc 2, Track 5
        - '2.01 Song.flac' -> Disc 2, Track 1
        - '02-03 Song.flac' -> Disc 2, Track 3
        - 'Side A1'        -> Disc 1, Track 1
        - 'Side B 02'      -> Disc 2, Track 2
        - 'CD 2 - 04'      -> Disc 2, Track 4
        - 'D2'             -> Disc 4, Track 2
        """
        assert parse_disc_and_track_number("1-01", "1-01 Track.flac")[:2] == (1, 1)
        assert parse_disc_and_track_number("2-05", "2-05 Track.flac")[:2] == (2, 5)
        assert parse_disc_and_track_number(None, "2.01 Track.flac")[:2] == (2, 1)
        assert parse_disc_and_track_number(None, "02-03 Track.flac")[:2] == (2, 3)
        assert parse_disc_and_track_number("A1", "A1 Vinyl.flac")[:2] == (1, 1)
        assert parse_disc_and_track_number("Side B 02", "Track.flac")[:2] == (2, 2)
        assert parse_disc_and_track_number(None, "CD 2 - 04 Track.flac")[:2] == (2, 4)
        assert parse_disc_and_track_number("D2", "D2 Vinyl.flac")[:2] == (4, 2)
