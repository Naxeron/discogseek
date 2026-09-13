"""Keep tests independent of the developer's .env, library, and service accounts."""

import pytest

from musicscraper.config import Config
from musicscraper.web import library, system


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    settings = {
        "CACHE_DIR": cache_dir,
        "MB_CACHE_DIR": cache_dir / "mb_cache",
        "AUDIO_CACHE_DB": cache_dir / "audio.db",
        "DEFAULT_LIBRARY_DIR": tmp_path / "music",
        "DEFAULT_OUTPUT_DIR": tmp_path / "downloads",
        "SLSKD_URL": "http://127.0.0.1:9",
        "SLSKD_API_KEY": "offline-test-key",
        "SLSKD_USERNAME": "",
        "SLSKD_PASSWORD": "",
        "NAVIDROME_URL": "",
        "NAVIDROME_USER": "",
        "NAVIDROME_USERNAME": "",
        "NAVIDROME_TOKEN": "",
        "NAVIDROME_PASSWORD": "",
        "NAVIDROME_SALT": "",
    }
    for key, value in settings.items():
        monkeypatch.setattr(Config, key, value)
    monkeypatch.setattr(Config, "save_to_env", lambda: True)
    monkeypatch.setattr(library, "_library_releases_cache", {"timestamp": 0, "releases": []})
    monkeypatch.setattr(system, "_last_status_cache", None)
