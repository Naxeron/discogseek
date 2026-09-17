"""Catalog builders shared by reconciliation tests."""

from discogseek.clients.musicbrainz import ArtistCatalog


def build_artist_catalog(artist_name="Test Artist", artist_id="art-1", aliases=None, releases=None):
    """Constructs a clean ArtistCatalog for testing."""
    alias_list = []
    if aliases:
        for a in aliases:
            alias_list.append({"alias": a, "name": a})

    rel_artist_data = []
    if releases:
        for rel in releases:
            tracks_data = []
            for t in rel.get("tracks", []):
                rec_id = t.get("rec_id") or f"rec-{t['title']}"
                trk_id = t.get("trk_id") or f"trk-{t['title']}"
                ac = t.get("artist_credit")
                if not ac:
                    ac = [{"artist": {"id": artist_id, "name": artist_name}}]
                tracks_data.append({
                    "id": trk_id,
                    "title": t["title"],
                    "number": str(t.get("number", "1")),
                    "position": int(t.get("number", "1")),
                    "recording": {
                        "id": rec_id,
                        "title": t["title"],
                    },
                    "artist-credit": ac,
                })
            rel_artist_data.append({
                "id": rel.get("id", f"rel-{rel['title']}"),
                "title": rel["title"],
                "release-group": {
                    "id": f"rg-{rel['title']}",
                    "title": rel["title"],
                    "primary-type": rel.get("type", "Album"),
                },
                "medium-list": [
                    {
                        "position": 1,
                        "track-list": tracks_data,
                    }
                ],
            })

    raw_data = {
        "artist": {
            "id": artist_id,
            "name": artist_name,
            "sort-name": artist_name,
            "alias-list": alias_list,
            "artist-relation-list": [],
            "url-relation-list": [],
        },
        "releases_artist": rel_artist_data,
        "releases_track_artist": [],
        "recordings": [],
    }
    return ArtistCatalog(raw_data)
