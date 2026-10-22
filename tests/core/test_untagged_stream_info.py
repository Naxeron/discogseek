"""Files without any tags (untagged peer downloads, WAV rips) keep their stream info."""

import struct
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from discogseek.core.audio import AUDIO_EXTRACTOR_VERSION, AudioMetadata, AudioQualityAnalyzer
from discogseek.core.cache import UnifiedCacheManager


def untagged_flac(path: Path, seconds: float) -> Path:
    info = bytearray(34)
    struct.pack_into(">HH", info, 0, 4096, 4096)
    info[10:18] = ((44100 << 44) | (1 << 41) | (15 << 36) | int(seconds * 44100)).to_bytes(8, "big")
    path.write_bytes(b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + bytes(info))
    return path


def untagged_mp3(path: Path, frames: int = 40) -> Path:
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz: 417-byte frames of 1152 samples, no ID3 tag.
    path.write_bytes((b"\xff\xfb\x90\x64" + b"\x00" * 413) * frames)
    return path


def test_untagged_flac_keeps_its_length(tmp_path):
    path = untagged_flac(tmp_path / "02 Beta.flac", 207.0)
    assert FLAC(str(path)).tags is None

    meta = AudioQualityAnalyzer.analyze_file(path)

    assert meta.duration == pytest.approx(207.0)
    assert meta.sample_rate == 44100
    assert meta.title == "Beta"
    assert meta.read_error is False


def test_untagged_mp3_keeps_its_length_bitrate_and_quality(tmp_path):
    meta = AudioQualityAnalyzer.analyze_file(untagged_mp3(tmp_path / "03 Gamma.mp3"))

    assert meta.duration == pytest.approx(40 * 1152 / 44100, abs=0.01)
    assert meta.bitrate_kbps == 128
    assert (meta.format_label, meta.quality_score) == ("MP3 128kbps", 30)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("duration, reread", [(0.0, True), (207.0, False)])
def test_cached_rows_that_lost_stream_info_are_reread_once(tmp_path, version, duration, reread):
    path = tmp_path / "02 Beta.flac"
    path.write_bytes(b"audio")
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    cache.store_audio_metadata_batch([AudioMetadata(
        path=path, title="Beta", track_number="2", duration=duration, extractor_version=version,
    )])

    assert (cache.get_audio_metadata(path) is None) is reread

    # A row written by the current extractor is trusted even without a length.
    cache.store_audio_metadata_batch([AudioMetadata(path=path, title="Beta", track_number="2")])
    cached = cache.get_audio_metadata(path)
    assert cached is not None and cached.extractor_version == AUDIO_EXTRACTOR_VERSION
