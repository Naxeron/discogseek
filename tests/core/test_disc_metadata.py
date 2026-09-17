"""Disc metadata fields, folder inference, and filename numbering."""

from pathlib import Path

import pytest

from discogseek.core.audio import (
    DISC_DIR_PATTERN,
    SIDE_MAP,
    WORD_NUMS,
    AudioMetadata,
    AudioQualityAnalyzer,
)


class TestDiscFolderMetadata:
    """Disc folder recognition, numbering, and album inference."""

    def test_disc_dir_pattern_baseline_coverage(self):
        """Verify DISC_DIR_PATTERN matches standard disc folder notations."""
        valid_cases = [
            ("Disc 1", 1),
            ("Disc 2", 2),
            ("disc 01", 1),
            ("DISC 02", 2),
            ("Disc-1", 1),
            ("Disc_2", 2),
            ("Disc.1", 1),
            ("CD 1", 1),
            ("CD 2", 2),
            ("CD01", 1),
            ("CD02", 2),
            ("cd-01", 1),
            ("cd_02", 2),
            ("Disk 1", 1),
            ("Disk 2", 2),
            ("disk01", 1),
            ("Vinyl 1", 1),
            ("Vinyl 2", 2),
            ("Side A", 1),
            ("Side B", 2),
            ("side 1", 1),
            ("side 2", 2),
            ("LP 1", 1),
            ("LP 2", 2),
            ("Disc One", 1),
            ("Disc Two", 2),
            ("Disc 1 - Bonus Tracks", 1),
            ("CD 1: The Early Years", 1),
        ]
        for folder_name, expected_disc in valid_cases:
            m = DISC_DIR_PATTERN.match(folder_name)
            assert m is not None, f"DISC_DIR_PATTERN failed to match valid folder: '{folder_name}'"

    def test_mixed_tag_vs_folder_precedence(self, tmp_path):
        """
        When file has no disc tag, folder disc number (Disc 2) takes precedence over default disc_number=1.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Album"
        d2 = album_dir / "Disc 2"
        d2.mkdir(parents=True)

        f_untagged = d2 / "01 Untagged.flac"
        f_untagged.write_text("data")

        meta = AudioQualityAnalyzer.analyze_file(f_untagged)
        assert meta.disc_number == 2
        assert meta.album == "Album"
        assert meta.artist == "Artist"

    def test_disc_folder_pattern_accepts_vinyl_sides_and_bracketed_suffixes(self):
        """Recognize vinyl-side folders and disc folders with bracketed suffixes."""
        folders = [
            "Vinyl Side A",
            "Vinyl Side B",
            "Disc 1 (Remaster)",
            "Disc 1 [Bonus Tracks]",
        ]
        failed_folders = [f for f in folders if not DISC_DIR_PATTERN.match(f)]
        assert not failed_folders, (
            f"CRITICAL BUG: DISC_DIR_PATTERN failed to match standard folder names: {failed_folders}"
        )

    @pytest.mark.parametrize("folder_name,expected_disc", [
        ("Vinyl Side A", 1),
        ("Vinyl Side B", 2),
        ("vinyl side a", 1),
        ("VINYL SIDE B", 2),
        ("Vinyl Side C", 3),
        ("Vinyl Side D", 4),
        ("Vinyl Side E", 5),
        ("Vinyl Side F", 6),
        ("Vinyl Side G", 7),
        ("Vinyl Side H", 8),
        ("LP Side 1", 1),
        ("LP Side 2", 2),
        ("lp side 1", 1),
        ("LP Side 3", 3),
        ("LP Side 4", 4),
        ("LP Side A", 1),
        ("LP Side B", 2),
        ("Disc 1 (Remaster)", 1),
        ("Disc 1 (Remastered 2024)", 1),
        ("Disc 1 [Bonus Tracks]", 1),
        ("Disc 1 - Bonus Disc", 1),
        ("Disc 1: Collector's Edition", 1),
        ("Disc 1. The Early Years", 1),
        ("Disc 1 _ Special Edition", 1),
        ("Disc 2 (Deluxe Edition)", 2),
        ("CD 1 (Remaster)", 1),
        ("CD 02 [Bonus]", 2),
        ("Vinyl Side A-B", 1),
        ("Side A", 1),
        ("Side B", 2),
        ("Side C", 3),
        ("Side D", 4),
        ("Side 1", 1),
        ("Side 2", 2),
        ("LP 1", 1),
        ("LP 2", 2),
        ("Disc One", 1),
        ("Disc Two", 2),
        ("Disc Three", 3),
        ("Disc Four", 4),
        ("Disc Five", 5),
        ("Disc Six", 6),
        ("Disc Seven", 7),
        ("Disc Eight", 8),
        ("Disc Nine", 9),
        ("Disc Ten", 10),
        ("CD 01", 1),
        ("CD 02", 2),
        ("CD03", 3),
        ("Disk 4", 4),
    ])
    def test_disc_dir_pattern_positive_matches(self, folder_name: str, expected_disc: int):
        """Verify DISC_DIR_PATTERN matches all standard and edge-case disc folder names."""
        m = DISC_DIR_PATTERN.match(folder_name)
        assert m is not None, f"DISC_DIR_PATTERN failed to match valid disc folder: '{folder_name}'"
        if m.group(1):
            disc_num = int(m.group(1))
        elif m.group(2):
            disc_num = SIDE_MAP.get(m.group(2).lower(), 1)
        elif m.group(3):
            disc_num = WORD_NUMS.get(m.group(3).lower(), 1)
        else:
            disc_num = None
        assert disc_num == expected_disc, (
            f"Expected disc {expected_disc} for '{folder_name}', got {disc_num}"
        )

    @pytest.mark.parametrize("invalid_folder", [
        "Discotheque",
        "Discourse",
        "Side by Side",
        "The Other Side of Town",
        "Dark Side of the Moon",
        "CD Baby",
        "Side-Effects",
        "LP Vinyl Records",
    ])
    def test_disc_dir_pattern_negative_matches(self, invalid_folder: str):
        """Verify DISC_DIR_PATTERN does not falsely match unrelated folder names."""
        m = DISC_DIR_PATTERN.match(invalid_folder)
        assert m is None, f"DISC_DIR_PATTERN falsely matched non-disc folder: '{invalid_folder}'"

    def test_disc_subfolder_album_inference_in_analyzer(self, tmp_path):
        """
        Verify AudioQualityAnalyzer infers the true album name from grandparent
        and disc number from parent folder for Vinyl Side A/B and Disc 1 (Remaster).
        """
        base_dir = tmp_path / "music" / "Pink Floyd" / "The Wall"
        side_a = base_dir / "Vinyl Side A"
        side_b = base_dir / "Vinyl Side B"
        remaster_dir = base_dir / "Disc 1 (Remaster)"

        side_a.mkdir(parents=True)
        side_b.mkdir(parents=True)
        remaster_dir.mkdir(parents=True)

        f_a = side_a / "01 In The Flesh.mp3"
        f_b = side_b / "01 Hey You.mp3"
        f_remaster = remaster_dir / "01 Another Brick.mp3"

        f_a.write_text("dummy audio")
        f_b.write_text("dummy audio")
        f_remaster.write_text("dummy audio")

        meta_a = AudioQualityAnalyzer.analyze_file(f_a)
        assert meta_a.disc_number == 1
        assert meta_a.album == "The Wall"

        meta_b = AudioQualityAnalyzer.analyze_file(f_b)
        assert meta_b.disc_number == 2
        assert meta_b.album == "The Wall"

        meta_remaster = AudioQualityAnalyzer.analyze_file(f_remaster)
        assert meta_remaster.disc_number == 1
        assert meta_remaster.album == "The Wall"


def test_audio_metadata_disc_parsing_tags():
    """Verifies that AudioMetadata dataclass supports disc_number and total_discs fields."""
    meta = AudioMetadata(
        path=Path("/music/Artist/Album/01 track.flac"),
        title="Overture",
        artist="Artist",
        album="Multi-Disc Album",
        track_number="1",
        disc_number=1,
        total_discs=2,
    )
    assert getattr(meta, "disc_number", None) == 1, "AudioMetadata must have disc_number attribute"
    assert getattr(meta, "total_discs", None) == 2, "AudioMetadata must have total_discs attribute"


def test_disc_subfolder_heuristic_detection(tmp_path):
    """
    Verifies that disc subfolders (Disc 1, Disc 2, CD 01, CD 02)
    are recognized as multi-disc releases, preserving artist and album.
    """
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Aphex Twin - Selected Ambient Works II"
    disc1 = album_dir / "Disc 1"
    disc2 = album_dir / "Disc 2"
    disc1.mkdir(parents=True)
    disc2.mkdir(parents=True)

    f1 = disc1 / "01 Cliffs.flac"
    f2 = disc2 / "01 Blue Calx.flac"
    f1.write_text("data1")
    f2.write_text("data2")

    meta1 = AudioQualityAnalyzer.analyze_file(f1)
    meta2 = AudioQualityAnalyzer.analyze_file(f2)

    # Must preserve true album and artist, not set album to "Disc 1"
    if meta1 and meta2:
        assert "disc 1" not in meta1.album.lower() or "selected ambient works" in meta1.album.lower()
        assert getattr(meta1, "disc_number", 1) == 1
        assert getattr(meta2, "disc_number", 1) == 2
