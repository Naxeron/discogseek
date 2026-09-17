"""Persist verified browser audits until their source inventory changes."""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from discogseek.core.cache import UnifiedCacheManager


# Bump this namespace when reconciliation rules change: old results must then be
# rechecked even when their source files have not changed.
_NAMESPACE = "library_browser_audits_v3"
_TRANSIENT_FIELDS = {
    "id", "browser_name_key", "browser_alias_keys", "navidrome_ids",
    "is_audited", "audit_error", "status", "found_count", "missing_count",
    "completion_pct",
}


def _encode(value: Any) -> Any:
    """Keep sets, tuples, and paths intact through the JSON cache."""
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (set, tuple, Path)):
        kind = "path" if isinstance(value, Path) else "set" if isinstance(value, set) else "tuple"
        items = str(value) if kind == "path" else [_encode(item) for item in value]
        if kind == "set":
            items.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {"__audit_cache_type__": kind, "value": items}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        kind = value.get("__audit_cache_type__")
        if kind == "set":
            return set(_decode(item) for item in value["value"])
        if kind == "tuple":
            return tuple(_decode(item) for item in value["value"])
        if kind == "path":
            return Path(value["value"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


class BrowserAuditCache:
    def __init__(self, cache: UnifiedCacheManager):
        self.cache = cache

    def key(self, release: Dict[str, Any]) -> str:
        inventory = {key: value for key, value in release.items() if key not in _TRANSIENT_FIELDS}
        for field in ("formats", "sources"):
            if inventory.get(field):
                inventory[field] = sorted(inventory[field])
        files = {}
        for track in release.get("tracks", []) + release.get("recording_candidates", []):
            path = track.get("path")
            if path and track.get("source", "local") != "navidrome":
                try:
                    stat = Path(path).stat()
                    files[str(path)] = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
                except OSError:
                    files[str(path)] = None
        payload = json.dumps(_encode([inventory, files]), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self.cache.get_api_cache(_NAMESPACE, key)
        if not isinstance(entry, dict):
            return None
        try:
            audit = _decode(entry["audit"])
            keys = entry["keys"]
            if (not isinstance(audit, dict) or not audit.get("is_audited")
                    or not isinstance(audit.get("tracks"), list) or not audit["tracks"]
                    or not isinstance(keys, list) or not all(isinstance(item, str) for item in keys)):
                return None
            if entry.get("generation", 0) != self._generation(audit.get("mb_release_id")):
                return None
            return {"audit": audit, "keys": keys}
        except (KeyError, TypeError, ValueError):
            return None

    def _generation(self, release_id: Optional[str]) -> int:
        value = self.cache.get_api_cache(_NAMESPACE + "_generations", release_id) if release_id else None
        return value if isinstance(value, int) else 0

    def invalidate(
        self, release: Dict[str, Any], previous: Optional[Dict[str, Any]], invalidated: Set[str],
    ) -> None:
        release_id = release.get("mb_release_id") or (previous["audit"].get("mb_release_id") if previous else None)
        if release_id and release_id not in invalidated:
            # Other snapshots of this edition (individual local/remote aliases
            # and their union) must also observe an explicit metadata refresh.
            self.cache.store_api_cache(
                _NAMESPACE + "_generations", release_id, self._generation(release_id) + 1,
            )
            invalidated.add(release_id)

    def store(
        self, key: str, release: Dict[str, Any], audited: Dict[str, Any],
        previous: Optional[Dict[str, Any]],
    ) -> None:
        keys = set(previous["keys"] if previous else []) | {key}
        if audited.get("is_audited") and audited.get("mb_release_id"):
            # Later scans can recognize an initially untagged release through
            # its remembered identity. Both forms refer to the same snapshot.
            resolved = dict(release, mb_release_id=audited["mb_release_id"])
            keys.add(self.key(resolved))
        entry = {
            "audit": _encode(audited), "keys": sorted(keys),
            "generation": self._generation(audited.get("mb_release_id")),
        } if audited.get("is_audited") else None
        for cache_key in keys:
            # A failed explicit refresh invalidates the old verified snapshot;
            # the next normal scan retries instead of resurrecting that result.
            self.cache.store_api_cache(_NAMESPACE, cache_key, entry)
