"""
Centralized configuration and environment settings for discogseek.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv, find_dotenv
    env_file = find_dotenv(usecwd=True)
    if not env_file:
        project_env = Path(__file__).resolve().parent.parent.parent / ".env"
        if project_env.exists():
            env_file = str(project_env)
    if env_file:
        load_dotenv(dotenv_path=env_file)
    else:
        load_dotenv()
except ImportError:
    pass


class Config:
    """Central application configuration with environment fallbacks."""

    # Default paths
    CACHE_DIR = Path(os.path.expanduser(os.environ.get("DISCOGSEEK_CACHE_DIR", "~/.cache/discogseek"))).resolve()
    MB_CACHE_DIR = CACHE_DIR / "mb_cache"
    AUDIO_CACHE_DB = CACHE_DIR / "discogseek_cache.db"

    # Default library and download paths
    DEFAULT_LIBRARY_DIR = Path(
        os.environ.get("DISCOGSEEK_LIBRARY_DIR", "/mnt/music" if os.path.exists("/mnt/music") else "./music")
    ).resolve()

    # User Agent
    USER_AGENT = (
        os.environ.get(
            "DISCOGSEEK_USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    )

    # MusicBrainz Application Identity
    MB_APP_NAME = "discogseek"
    MB_APP_VERSION = "1.0"
    MB_APP_CONTACT = "https://github.com/naxeron"

    # Soulseek / slskd Settings
    SLSKD_URL = os.environ.get("SLSKD_URL", "http://localhost:5030").rstrip("/")
    SLSKD_USERNAME = os.environ.get("SLSKD_USERNAME")
    SLSKD_PASSWORD = os.environ.get("SLSKD_PASSWORD")
    SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY")

    # Navidrome / Subsonic Settings
    NAVIDROME_URL = (
        os.environ.get("NAVIDROME_URL")
        or os.environ.get("SUBSONIC_URL")
        or ""
    ).rstrip("/")
    NAVIDROME_USER = (
        os.environ.get("NAVIDROME_USERNAME")
        or os.environ.get("NAVIDROME_USER")
        or os.environ.get("SUBSONIC_USERNAME")
        or os.environ.get("SUBSONIC_USER")
        or ""
    )
    NAVIDROME_USERNAME = NAVIDROME_USER
    NAVIDROME_TOKEN = (
        os.environ.get("NAVIDROME_PASSWORD")
        or os.environ.get("NAVIDROME_TOKEN")
        or os.environ.get("NAVIDROME_PASS")
        or os.environ.get("SUBSONIC_PASSWORD")
        or os.environ.get("SUBSONIC_TOKEN")
        or ""
    )
    NAVIDROME_PASSWORD = NAVIDROME_TOKEN
    NAVIDROME_SALT = (
        os.environ.get("NAVIDROME_SALT")
        or os.environ.get("SUBSONIC_SALT")
        or ""
    )
