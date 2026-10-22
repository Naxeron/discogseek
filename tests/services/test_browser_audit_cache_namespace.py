"""Audits cached under earlier reconciliation rules are recomputed."""

from discogseek.core.cache import UnifiedCacheManager
from discogseek.services import library_browser_cache
from discogseek.services.library_browser_cache import BrowserAuditCache


def test_audits_from_previous_rules_are_not_reused(tmp_path):
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    audits = BrowserAuditCache(cache)
    release = {"artist": "Machine Drum", "title": "Now You Know", "mb_release_id": "vinyl", "tracks": []}
    key = audits.key(release)
    stale = {"is_audited": True, "mb_release_id": "vinyl", "missing_count": 8,
             "tracks": [{"title": "Hello My Future", "status": "missing"}]}
    # v5: a subtitle naming another version ('(Bleep version)') no longer matches at position.
    previous = ("library_browser_audits_v3", "library_browser_audits_v4", "library_browser_audits_v5")
    for namespace in previous:
        cache.store_api_cache(namespace, key, {"audit": stale, "keys": [key], "generation": 0})

    assert audits.load(key) is None
    assert library_browser_cache._NAMESPACE not in previous

    audits.store(key, release, dict(stale, missing_count=0), None)
    assert audits.load(key)["audit"]["missing_count"] == 0
