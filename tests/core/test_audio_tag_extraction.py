"""Tag extraction: fixed key precedence, MP4/APEv2/ID3 value forms, totals and read errors."""

import os
import struct
import subprocess
import sys
from pathlib import Path

import mutagen
import pytest
from mutagen._vorbis import VCommentDict
from mutagen.apev2 import APEv2
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TPE1, TPE2, TRCK, TXXX, UFID
from mutagen.mp4 import MP4FreeForm, MP4Tags

from discogseek.core import audio
from discogseek.core.audio import AudioQualityAnalyzer


def write_flac(path: Path, tags) -> Path:
    """A tagged FLAC file holding only a STREAMINFO block (no audio frames)."""
    info = bytearray(34)
    struct.pack_into(">HH", info, 0, 4096, 4096)
    info[10:18] = ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big")
    path.write_bytes(b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + bytes(info))
    flac = FLAC(str(path))
    for key, value in tags.items():
        flac[key] = value
    flac.save()
    return path


DUAL_CREDITS = {
    "ALBUMARTIST": "Machine Drum", "ALBUM ARTIST": "Machinedrum",
    "ARTIST": "Squarepusher", "PERFORMER": "Tom Jenkinson",
    "DATE": "2004-10", "YEAR": "2004", "ALBUM": "Bidnezz", "TITLE": "Wiggle",
    "TRACKNUMBER": "12", "TRACKTOTAL": "22",
}


@pytest.mark.parametrize("order", [sorted, lambda keys: sorted(keys, reverse=True)],
                         ids=["sorted-keys", "reverse-sorted-keys"])
def test_vorbis_field_precedence_does_not_depend_on_key_order(tmp_path, monkeypatch, order):
    path = write_flac(tmp_path / "12 Wiggle.flac", DUAL_CREDITS)
    original = VCommentDict.keys
    monkeypatch.setattr(VCommentDict, "keys", lambda self: order(original(self)))

    meta = AudioQualityAnalyzer.analyze_file(path)

    assert meta.album_artist == "Machine Drum"
    assert meta.artist == "Squarepusher"
    assert meta.year == "2004-10"
    assert (meta.track_number, meta.track_total) == ("12", 22)
    assert meta.read_error is False


def test_vorbis_precedence_is_stable_across_hash_seeds(tmp_path):
    path = write_flac(tmp_path / "12 Wiggle.flac", DUAL_CREDITS)
    script = (
        "import sys; from pathlib import Path; "
        "from discogseek.core.audio import AudioQualityAnalyzer as A; "
        "m = A.analyze_file(Path(sys.argv[1])); print(m.album_artist, '|', m.artist, '|', m.year)"
    )
    src = str(Path(audio.__file__).resolve().parents[2])
    outputs = set()
    for seed in range(1, 7):
        result = subprocess.run(
            [sys.executable, "-c", script, str(path)], capture_output=True, text=True, check=True,
            env=dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=src),
        )
        outputs.add(result.stdout.strip())
    assert outputs == {"Machine Drum | Squarepusher | 2004-10"}


class FakeFile:
    def __init__(self, tags):
        self.tags = tags
        self.info = None


def analyze_tags(monkeypatch, tags, name="01 Track.m4a"):
    monkeypatch.setattr(mutagen, "File", lambda *_args, **_kwargs: FakeFile(tags))
    return AudioQualityAnalyzer.analyze_file(Path("/music/Artist/Album") / name)


def test_mp4_freeform_musicbrainz_ids_and_track_and_disc_pairs(monkeypatch):
    tags = MP4Tags()
    tags["\xa9nam"] = ["FACT Mix 120"]
    tags["\xa9ART"] = ["Lone"]
    tags["aART"] = ["Lone"]
    tags["\xa9alb"] = ["FACT Mix 120: Lone"]
    tags["trkn"] = [(3, 10)]
    tags["disk"] = [(2, 2)]
    tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [MP4FreeForm(b"87ef0ed7-release")]
    tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [MP4FreeForm(b"fbcbba8d-recording")]
    tags["----:com.apple.iTunes:MusicBrainz Release Track Id"] = [MP4FreeForm(b"c0ffee-track")]
    tags["----:com.apple.iTunes:MusicBrainz Artist Id"] = [MP4FreeForm(b"artist-1")]

    meta = analyze_tags(monkeypatch, tags)

    assert meta.mb_release_ids == {"87ef0ed7-release"}
    assert meta.mb_rec_ids == {"fbcbba8d-recording"}
    assert meta.mb_track_ids == {"c0ffee-track"}
    assert meta.mb_artist_ids == {"artist-1"}
    assert (meta.track_number, meta.track_total) == ("3", 10)
    assert (meta.disc_number, meta.total_discs) == (2, 2)
    assert (meta.title, meta.artist, meta.album_artist) == ("FACT Mix 120", "Lone", "Lone")


def test_apev2_track_key_and_totals(monkeypatch):
    tags = APEv2()
    tags["Track"] = "3/6"
    tags["Disc"] = "1/1"
    tags["Album Artist"] = "Squarepusher"
    tags["Artist"] = "Squarepusher\x00Guest"
    tags["Musicbrainz_Albumid"] = "cfd3b99b-release"

    meta = analyze_tags(monkeypatch, tags, name="03 Numbers Lucent.wv")

    assert (meta.track_number, meta.track_total) == ("3", 6)
    assert meta.album_artist == "Squarepusher"
    assert meta.artist == "Squarepusher"
    assert meta.mb_release_ids == {"cfd3b99b-release"}


def test_id3_multi_value_frames_are_separate_values(monkeypatch):
    tags = ID3()
    tags.add(TPE1(encoding=3, text=["Lone", "Guest"]))
    tags.add(TPE2(encoding=3, text=["Lone"]))
    tags.add(TRCK(encoding=3, text=["3/12"]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=["release-a", "release-b"]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=["artist-a", "artist-b"]))
    tags.add(UFID(owner="http://musicbrainz.org", data=b"recording-1"))

    meta = analyze_tags(monkeypatch, tags, name="03 Song.mp3")

    assert meta.artist == "Lone"
    assert "\x00" not in meta.artist
    assert meta.mb_release_ids == {"release-a", "release-b"}
    assert meta.mb_artist_ids == {"artist-a", "artist-b"}
    assert meta.mb_rec_ids == {"recording-1"}
    assert (meta.track_number, meta.track_total) == ("3", 12)


@pytest.mark.parametrize("failure", ["raises", "unreadable"])
def test_unreadable_audio_is_marked_as_a_read_error(monkeypatch, failure):
    def broken(*_args, **_kwargs):
        if failure == "raises":
            raise OSError("Input/output error")
        return None

    monkeypatch.setattr(mutagen, "File", broken)
    meta = AudioQualityAnalyzer.analyze_file(Path("/music/Artist/Some Folder/01 Song.flac"))

    assert meta.read_error is True
    # The scan may still show the folder/filename fallback for this run.
    assert meta.album == "Some Folder"


@pytest.mark.parametrize("credit", ["ＶＡＲＩＯＵＳ", "V.A", "Various Artists"])
def test_various_artists_album_credits_are_normalized(tmp_path, credit):
    path = write_flac(tmp_path / "01 Song.flac", {"ALBUMARTIST": credit, "ARTIST": "Lazhward", "ALBUM": "Split"})

    assert AudioQualityAnalyzer.analyze_file(path).album_artist == "Various Artists"
