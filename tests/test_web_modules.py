"""Exercise API module boundaries and asset routing without a listening socket."""

import hashlib
import io
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from musicscraper.services.library import LibraryReleaseService
from musicscraper.web import api
from musicscraper.web.server import MusicScraperHTTPRequestHandler, STATIC_DIR


class MemoryConnection:
    def __init__(self, path):
        self.request = io.BytesIO(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        self.response = io.BytesIO()

    def makefile(self, *args):
        return self.request

    def sendall(self, data):
        self.response.write(data)


def get_asset(path):
    connection = MemoryConnection(path)
    MusicScraperHTTPRequestHandler(connection, ("127.0.0.1", 0), Mock())
    headers, body = connection.response.getvalue().split(b"\r\n\r\n", 1)
    return headers.decode(), body


def test_javascript_import_graph_is_served():
    """Every transitive entry-point import must be packaged under a served URL."""
    class PageScripts(HTMLParser):
        scripts = None

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.scripts.append(dict(attrs))

    parser = PageScripts()
    parser.scripts = []
    headers, body = get_asset("/")
    assert "200 OK" in headers
    parser.feed(body.decode())
    assert parser.scripts
    assert all(script.get("type") == "module" for script in parser.scripts)
    pending = [script["src"] for script in parser.scripts]
    visited = set()
    while pending:
        url = pending.pop()
        if url in visited:
            continue
        visited.add(url)
        headers, body = get_asset(url)
        assert "200 OK" in headers, url
        assert "application/javascript" in headers
        filename = url[len("/static/"):] if url.startswith("/static/") else url.lstrip("/")
        assert body == (STATIC_DIR / filename).read_bytes()
        for imported in re.findall(r"\bfrom\s+['\"]([^'\"]+)['\"]", body.decode()):
            assert imported.startswith("/static/js/"), imported
            pending.append(imported)
    assert len(visited) > 1


@pytest.mark.parametrize("url", [
    "/static/js/../../../outside.js",
    "/static/js/%2e%2e/%2e%2e/outside.js",
])
def test_module_route_confines_paths_to_static_directory(url):
    headers, _ = get_asset(url)
    assert "403 Forbidden" in headers


@pytest.mark.parametrize("identifier", ["local-id", "mb-id", hashlib.md5(b"mb_mb-id").hexdigest()[:16]])
def test_background_scan_publishes_release_snapshot(monkeypatch, identifier):
    release = {"id": "local-id", "mb_release_id": "mb-id", "artist": "Artist", "title": "Album", "tracks": []}
    scan = Mock(return_value=[release])
    monkeypatch.setattr(LibraryReleaseService, "scan_library_releases", scan)
    api.run_library_scan_task(Mock(params={}))

    assert api.get_library_release_details(identifier, audit=False) == release
    assert api.get_library_releases()["count"] == 1
    scan.assert_called_once()


def test_audit_groups_tracks_by_id_then_title(monkeypatch):
    from musicscraper.services.auditor import AuditorService

    tracks = [
        {"title": "Found", "release_id": "second", "release_title": "Same Title"},
        {"title": "Missing", "release_title": "same title"},
    ]
    catalog = SimpleNamespace(
        name="Artist", sort_name="Artist", mbid="artist-id", artist_info={}, bandcamp_urls=[],
        releases=[{"id": "first", "title": "Same Title"}, {"id": "second", "title": "Same Title"}],
        tracks=tracks,
    )
    audit = Mock(return_value=(catalog, [{"mb_track": tracks[0], "local_track": {"filename": "found.flac"}}], [tracks[1]]))
    monkeypatch.setattr(AuditorService, "audit_artist", audit)
    result = api.run_audit_task(Mock(params={"artist": "Artist"}))
    first, second = result["releases"]
    assert first["missing_count"] == first["total_tracks"] == 1
    assert second["found_count"] == second["total_tracks"] == 1
    assert second["tracks"][0]["local_file"] == "found.flac"
    assert result["completion_pct"] == 50.0
