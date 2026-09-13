"""Service health checks and runtime settings for the web API."""

import time
from pathlib import Path
from typing import Any, Dict, Optional

from musicscraper.config import Config


_last_status_cache: Optional[Dict[str, Any]] = None
_last_status_time: float = 0.0
_STATUS_CACHE_TTL = 5.0  # seconds


def get_system_status(force: bool = False) -> Dict[str, Any]:
    """Inspects and returns the status of connected services and local paths."""
    global _last_status_cache, _last_status_time
    now = time.time()
    if not force and _last_status_cache is not None and (now - _last_status_time) < _STATUS_CACHE_TTL:
        return _last_status_cache

    status = {
        "timestamp": now,
        "paths": {
            "library_dir": str(Config.DEFAULT_LIBRARY_DIR),
            "library_exists": Config.DEFAULT_LIBRARY_DIR.exists(),
            "output_dir": str(Config.DEFAULT_OUTPUT_DIR),
            "output_exists": Config.DEFAULT_OUTPUT_DIR.exists(),
            "cache_dir": str(Config.CACHE_DIR),
            "cache_exists": Config.CACHE_DIR.exists(),
        },
        "services": {
            "slskd": {"configured": bool(Config.SLSKD_URL), "url": Config.SLSKD_URL, "connected": False, "username": None, "state": None},
            "navidrome": {"configured": bool(Config.NAVIDROME_URL and (Config.NAVIDROME_USER or getattr(Config, "NAVIDROME_USERNAME", ""))), "url": Config.NAVIDROME_URL, "connected": False},
            "musicbrainz": {"configured": True, "connected": True},
        }
    }

    # Check slskd
    try:
        from musicscraper.clients.slskd import SlskdClient
        client = SlskdClient()
        app_info = client.get_application()
        if app_info:
            status["services"]["slskd"]["connected"] = True
            status["services"]["slskd"]["username"] = app_info.get("user", {}).get("username")
            status["services"]["slskd"]["state"] = app_info.get("server", {}).get("state")
    except Exception as e:
        status["services"]["slskd"]["error"] = str(e)

    # Check Navidrome
    nav_user = Config.NAVIDROME_USER or getattr(Config, "NAVIDROME_USERNAME", "")
    nav_pass = Config.NAVIDROME_TOKEN or getattr(Config, "NAVIDROME_PASSWORD", "")
    if Config.NAVIDROME_URL and nav_user:
        try:
            from musicscraper.clients.navidrome import NavidromeScanner
            nav = NavidromeScanner(base_url=Config.NAVIDROME_URL, username=nav_user, password=nav_pass, timeout=4)
            is_connected = nav.test_connection()
            status["services"]["navidrome"]["connected"] = is_connected
            if not is_connected and getattr(nav, "last_error", None):
                status["services"]["navidrome"]["error"] = nav.last_error
        except Exception as e:
            status["services"]["navidrome"]["error"] = str(e)

    _last_status_cache = status
    _last_status_time = now
    return status


def get_system_config() -> Dict[str, Any]:
    """Returns current environment configurations."""
    nav_user = Config.NAVIDROME_USER or getattr(Config, "NAVIDROME_USERNAME", "")
    nav_token = Config.NAVIDROME_TOKEN or getattr(Config, "NAVIDROME_PASSWORD", "")
    return {
        "DEFAULT_LIBRARY_DIR": str(Config.DEFAULT_LIBRARY_DIR),
        "DEFAULT_OUTPUT_DIR": str(Config.DEFAULT_OUTPUT_DIR),
        "CACHE_DIR": str(Config.CACHE_DIR),
        "SLSKD_URL": Config.SLSKD_URL,
        "SLSKD_USERNAME": Config.SLSKD_USERNAME or "",
        "has_slskd_password": bool(Config.SLSKD_PASSWORD),
        "NAVIDROME_URL": Config.NAVIDROME_URL,
        "NAVIDROME_USER": nav_user,
        "NAVIDROME_USERNAME": nav_user,
        "has_navidrome_token": bool(nav_token),
        "has_navidrome_password": bool(nav_token),
    }


def update_system_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Updates runtime configuration settings and persists to .env."""
    if not isinstance(updates, dict):
        raise ValueError("Configuration updates must be a JSON object")

    if "DEFAULT_LIBRARY_DIR" in updates and updates["DEFAULT_LIBRARY_DIR"] is not None:
        Config.DEFAULT_LIBRARY_DIR = Path(str(updates["DEFAULT_LIBRARY_DIR"])).resolve()
    if "DEFAULT_OUTPUT_DIR" in updates and updates["DEFAULT_OUTPUT_DIR"] is not None:
        Config.DEFAULT_OUTPUT_DIR = Path(str(updates["DEFAULT_OUTPUT_DIR"])).resolve()
    if "SLSKD_URL" in updates and updates["SLSKD_URL"] is not None:
        Config.SLSKD_URL = str(updates["SLSKD_URL"]).rstrip("/")
    if "SLSKD_USERNAME" in updates and updates["SLSKD_USERNAME"] is not None:
        Config.SLSKD_USERNAME = str(updates["SLSKD_USERNAME"])
    # Only update password if a non-empty string is passed (preserves existing password)
    if updates.get("SLSKD_PASSWORD"):
        Config.SLSKD_PASSWORD = str(updates["SLSKD_PASSWORD"])
    if "NAVIDROME_URL" in updates and updates["NAVIDROME_URL"] is not None:
        Config.NAVIDROME_URL = str(updates["NAVIDROME_URL"]).rstrip("/")
    if "NAVIDROME_USER" in updates or "NAVIDROME_USERNAME" in updates:
        user_val = str(updates.get("NAVIDROME_USER") or updates.get("NAVIDROME_USERNAME") or "")
        Config.NAVIDROME_USER = user_val
        Config.NAVIDROME_USERNAME = user_val
    if updates.get("NAVIDROME_TOKEN"):
        token_val = str(updates["NAVIDROME_TOKEN"])
        Config.NAVIDROME_TOKEN = token_val
        Config.NAVIDROME_PASSWORD = token_val
    elif updates.get("NAVIDROME_PASSWORD"):
        token_val = str(updates["NAVIDROME_PASSWORD"])
        Config.NAVIDROME_TOKEN = token_val
        Config.NAVIDROME_PASSWORD = token_val

    global _last_status_cache
    _last_status_cache = None

    Config.save_to_env()

    return get_system_config()


def get_slskd_transfers() -> Dict[str, Any]:
    """Fetches active downloads, uploads, and search states from slskd."""
    try:
        from musicscraper.clients.slskd import SlskdClient
        client = SlskdClient()
        downloads = client.get_all_downloads()
        return {
            "connected": True,
            "downloads": downloads or []
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
            "downloads": []
        }
