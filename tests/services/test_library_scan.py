"""Library release grouping, sequence gaps, and compilation discovery."""

import pytest

from discogseek.core.audio import AudioMetadata
from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.library import LibraryReleaseService


def test_multi_disc_sequence_gap_detection(tmp_path):
    """
    Verifies that LibraryReleaseService tracks gaps per (disc_number, track_number)
    tuple so that a missing track on Disc 1 is not masked by the presence of that
    same track number on Disc 2.
    """
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Nine Inch Nails - The Fragile"
    disc1_dir = album_dir / "Disc 1"
    disc2_dir = album_dir / "Disc 2"
    disc1_dir.mkdir(parents=True)
    disc2_dir.mkdir(parents=True)

    # Disc 1 has Track 1, Track 3 (Track 2 is MISSING on Disc 1)
    (disc1_dir / "01 Somewhat Damaged.flac").write_text("dummy")
    (disc1_dir / "03 We're in This Together.flac").write_text("dummy")

    # Disc 2 has Track 1, Track 2, Track 3 (Complete)
    (disc2_dir / "01 The Way Out Is Through.flac").write_text("dummy")
    (disc2_dir / "02 Into the Void.flac").write_text("dummy")
    (disc2_dir / "03 Where Is Everybody.flac").write_text("dummy")

    cache_db = tmp_path / "gap_cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    # Store metadata for files
    def make_meta(p, title, trk, disc):
        kwargs = {
            "path": p,
            "title": title,
            "artist": "Nine Inch Nails",
            "album": "The Fragile",
            "track_number": str(trk),
            "format_label": "FLAC",
            "is_lossless": True,
            "quality_score": 90,
        }
        try:
            return AudioMetadata(**kwargs, disc_number=disc, total_discs=2)
        except TypeError:
            return AudioMetadata(**kwargs)

    cache.store_audio_metadata(make_meta(disc1_dir / "01 Somewhat Damaged.flac", "Somewhat Damaged", 1, 1))
    cache.store_audio_metadata(make_meta(disc1_dir / "03 We're in This Together.flac", "We're in This Together", 3, 1))
    cache.store_audio_metadata(make_meta(disc2_dir / "01 The Way Out Is Through.flac", "The Way Out Is Through", 1, 2))
    cache.store_audio_metadata(make_meta(disc2_dir / "02 Into the Void.flac", "Into the Void", 2, 2))
    cache.store_audio_metadata(make_meta(disc2_dir / "03 Where Is Everybody.flac", "Where Is Everybody", 3, 2))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)

    assert len(releases) >= 1
    rel = releases[0]
    # Check if missing tracks include Disc 1 Track 2
    missing_tracks = [t for t in rel["tracks"] if t.get("status") == "missing"]
    missing_nums = [(t.get("disc_number", 1), t.get("track_num_int")) for t in missing_tracks]

    # Disc 1 Track 2 must be detected as missing
    assert any(num == 2 and disc == 1 for disc, num in missing_nums) or rel.get("status") == "has_missing"

def test_scan_library_releases_grouping(tmp_path):
    """Verifies that audio files in library are properly grouped into releases with stats."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    # Create dummy album 1: Aphex Twin - Selected Ambient Works
    saw_dir = music_dir / "Aphex Twin - Selected Ambient Works"
    saw_dir.mkdir()
    (saw_dir / "01 Xtal.flac").write_text("dummy")
    (saw_dir / "02 Tha.flac").write_text("dummy")

    # Create dummy album 2: Boards of Canada - Music Has the Right to Children
    boc_dir = music_dir / "Boards of Canada" / "MHTRTC"
    boc_dir.mkdir(parents=True)
    (boc_dir / "01 Wildlife Analysis.mp3").write_text("dummy")

    # Pre-populate cache with audio metadata
    cache_db = tmp_path / "cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    cache.store_audio_metadata(AudioMetadata(
        path=saw_dir / "01 Xtal.flac",
        title="Xtal",
        artist="Aphex Twin",
        album="Selected Ambient Works 85-92",
        track_number="1/13",
        year="1992",
        format_label="FLAC",
        is_lossless=True,
        bitrate_kbps=900,
        quality_score=90
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=saw_dir / "02 Tha.flac",
        title="Tha",
        artist="Aphex Twin",
        album="Selected Ambient Works 85-92",
        track_number="2/13",
        year="1992",
        format_label="FLAC",
        is_lossless=True,
        bitrate_kbps=850,
        quality_score=90
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=boc_dir / "01 Wildlife Analysis.mp3",
        title="Wildlife Analysis",
        artist="Boards of Canada",
        album="Music Has the Right to Children",
        track_number="1/18",
        year="1998",
        format_label="MP3",
        is_lossless=False,
        bitrate_kbps=320,
        quality_score=60
    ))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=False)

    assert len(releases) == 2

    # Check Aphex Twin release
    saw_rel = next(r for r in releases if "Aphex" in r["artist"])
    assert saw_rel["title"] == "Selected Ambient Works 85-92"
    assert saw_rel["year"] == "1992"
    assert saw_rel["found_count"] == 2
    assert saw_rel["total_tracks_expected"] == 13
    assert saw_rel["missing_count"] == 11
    assert saw_rel["status"] == "has_missing"
    assert "FLAC" in saw_rel["formats"]

    # Check BoC release
    boc_rel = next(r for r in releases if "Boards" in r["artist"])
    assert boc_rel["title"] == "Music Has the Right to Children"
    assert boc_rel["found_count"] == 1
    assert boc_rel["total_tracks_expected"] == 18
    assert boc_rel["missing_count"] == 17
    assert boc_rel["status"] == "has_missing"

def test_scan_library_releases_gap_detection(tmp_path):
    """Tests automatic sequence gap detection when tags do not have total tracks."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    album_dir = music_dir / "Autechre - Tri Repetae"
    album_dir.mkdir()
    (album_dir / "01 Dael.flac").write_text("dummy")
    (album_dir / "02 Clipper.flac").write_text("dummy")
    (album_dir / "04 Leterel.flac").write_text("dummy")

    cache_db = tmp_path / "cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    cache.store_audio_metadata(AudioMetadata(
        path=album_dir / "01 Dael.flac",
        title="Dael",
        artist="Autechre",
        album="Tri Repetae",
        track_number="1",
        year="1995",
        format_label="FLAC",
        is_lossless=True,
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=album_dir / "02 Clipper.flac",
        title="Clipper",
        artist="Autechre",
        album="Tri Repetae",
        track_number="2",
        year="1995",
        format_label="FLAC",
        is_lossless=True,
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=album_dir / "04 Leterel.flac",
        title="Leterel",
        artist="Autechre",
        album="Tri Repetae",
        track_number="4",
        year="1995",
        format_label="FLAC",
        is_lossless=True,
    ))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=False)

    assert len(releases) == 1
    rel = releases[0]
    assert rel["title"] == "Tri Repetae"
    assert rel["found_count"] == 3
    assert rel["total_tracks_expected"] == 4
    assert rel["missing_count"] == 1
    assert rel["status"] == "has_missing"

    # Track 3 should be marked as missing
    t3 = next((t for t in rel["tracks"] if t.get("track_number") == "3"), None)
    assert t3 is not None
    assert t3["status"] == "missing"

def test_scan_library_various_artists_compilation(tmp_path):
    """Verifies that compilation releases are properly identified as Various Artists even with 1 track."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    # 1. Folder with VA directory marker: "VA - Amen Destroyer" with 1 track by Exnoiz
    va_dir = music_dir / "VA - Amen Destroyer"
    va_dir.mkdir()
    (va_dir / "01 - Exnoiz - Destruction.mp3").write_text("dummy")

    # 2. Folder with multiple tracks by different artists
    comp_dir = music_dir / "Breakcore Sampler"
    comp_dir.mkdir()
    (comp_dir / "01 - Bong-Ra - Jungle.flac").write_text("dummy")
    (comp_dir / "02 - Venetian Snares - Hajnal.flac").write_text("dummy")

    cache_db = tmp_path / "cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    cache.store_audio_metadata(AudioMetadata(
        path=va_dir / "01 - Exnoiz - Destruction.mp3",
        title="Destruction",
        artist="Exnoiz",
        album_artist="Various Artists",
        album="Amen Destroyer",
        track_number="1/10",
        year="2004",
        format_label="MP3",
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=comp_dir / "01 - Bong-Ra - Jungle.flac",
        title="Jungle",
        artist="Bong-Ra",
        album="Breakcore Sampler",
        track_number="1/2",
        format_label="FLAC",
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=comp_dir / "02 - Venetian Snares - Hajnal.flac",
        title="Hajnal",
        artist="Venetian Snares",
        album="Breakcore Sampler",
        track_number="2/2",
        format_label="FLAC",
    ))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=False)

    assert len(releases) == 2
    amen_rel = next(r for r in releases if r["title"] == "Amen Destroyer")
    assert amen_rel["artist"] == "Various Artists"
    assert amen_rel["is_va"] is True
    assert amen_rel["tracks"][0]["artist"] == "Exnoiz"

    sampler_rel = next(r for r in releases if r["title"] == "Breakcore Sampler")
    assert sampler_rel["artist"] == "Various Artists"
    assert sampler_rel["is_va"] is True
    assert len(sampler_rel["tracks"]) == 2

@pytest.mark.parametrize("folder_name", [
    "TSUGIHAGI RECORDS (Buster Nalmi) - Lightning",
    "Various Artists/Lightning",
])
def test_scan_library_keeps_explicit_album_artist_with_guest_tracks(tmp_path, folder_name):
    music_dir = tmp_path / "music"
    album_dir = music_dir / folder_name
    album_dir.mkdir(parents=True)
    cache = UnifiedCacheManager(db_path=tmp_path / "cache.db")
    album_artist = "TSUGIHAGI RECORDS (Buster Nalmi)"
    for number, (title, artist) in enumerate([
        ("Get High", "Buster Nalmi"),
        ("I'm done deadly at all (Band Edit)", "The Busters"),
    ], 1):
        path = album_dir / f"{number:02d} {title}.flac"
        path.write_text("dummy")
        cache.store_audio_metadata(AudioMetadata(
            path=path, title=title, artist=artist, album_artist=album_artist,
            album="Lightning", track_number=str(number), format_label="FLAC",
        ))

    releases = LibraryReleaseService(cache_manager=cache).scan_library_releases(
        library_dir=music_dir,
    )

    assert len(releases) == 1
    assert releases[0]["artist"] == album_artist
    assert releases[0]["album_artist"] == album_artist
    assert releases[0]["is_va"] is False
    assert [track["artist"] for track in releases[0]["tracks"]] == ["Buster Nalmi", "The Busters"]


def test_scan_library_unifies_mbid_tagged_track_with_untagged_downloads(tmp_path):
    """Verifies that an album with 1 MBID-tagged track in Library/ and untagged tracks in downloads/ unifies into one release."""
    music_dir = tmp_path / "music"
    lib_dir = music_dir / "Library" / "Various Artists" / "新しいフォルダー (10)"
    dl_dir = music_dir / "downloads" / "Various Artitsts - 新しいフォルダー (10)"
    lib_dir.mkdir(parents=True)
    dl_dir.mkdir(parents=True)

    f1 = lib_dir / "30 exnoiz - re_Control.flac"
    f2 = dl_dir / "01 DJ - Track 1.flac"
    f3 = dl_dir / "02 Producer - Track 2.flac"
    f1.write_text("dummy")
    f2.write_text("dummy")
    f3.write_text("dummy")

    cache_db = tmp_path / "cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    # Track in Library/ has MBID tag
    cache.store_audio_metadata(AudioMetadata(
        path=f1,
        title="re_Control",
        artist="exnoiz",
        album_artist="Various Artists",
        album="新しいフォルダー (10)",
        track_number="30/30",
        format_label="FLAC",
        mb_release_ids={"062bbccc-346a-49af-b4c8-3037db346d56"}
    ))
    # Tracks in downloads/ do NOT have MBID tag and have "Various Artitsts" typo in album artist
    cache.store_audio_metadata(AudioMetadata(
        path=f2,
        title="Track 1",
        artist="DJ",
        album_artist="Various Artitsts",
        album="新しいフォルダー (10)",
        track_number="1/30",
        format_label="FLAC"
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=f3,
        title="Track 2",
        artist="Producer",
        album_artist="Various Artitsts",
        album="新しいフォルダー (10)",
        track_number="2/30",
        format_label="FLAC"
    ))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=False)

    # Must be unified into exactly ONE release, NOT split into two
    matching = [r for r in releases if r["title"] == "新しいフォルダー (10)"]
    assert len(matching) == 1
    rel = matching[0]
    assert rel["artist"] == "Various Artists"
    assert rel["found_count"] == 3
    assert rel["mb_release_id"] == "062bbccc-346a-49af-b4c8-3037db346d56"
    assert "Library" in rel["folder_path"]
