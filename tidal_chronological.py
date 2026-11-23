"""Sync saved TIDAL tracks into a playlist ordered by catalog release date."""

import argparse
import json
import re
import sys
from datetime import date
from urllib.parse import quote
from uuid import uuid4

from tidal_audit import API, COLLECTION, Tidal, attributes, request, settings, sign_in

NAME = "All of my songs chronologically"


def batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def catalog_dates(pages):
    """Yield each track and the earliest release date among its linked albums."""
    for page in pages:
        included = page.get("included") or []
        albums = {}
        for album in included:
            if album["type"] == "albums":
                try:
                    albums[album["id"]] = date.fromisoformat(attributes(album)["releaseDate"])
                except (KeyError, TypeError, ValueError):
                    continue

        for track in [*page["data"], *included]:
            if track["type"] != "tracks" or "attributes" not in track:
                continue
            linked = (track.get("relationships") or {}).get("albums", {}).get("data") or []
            earliest = min((albums[item["id"]] for item in linked if item["id"] in albums),
                           default=None)
            yield track["id"], attributes(track).get("isrc"), earliest


def collection_order(api, country):
    saved = {}
    tracks = {}
    for page in api.pages(COLLECTION, countryCode=country, include="items.albums"):
        saved.update(dict.fromkeys(item["id"] for item in page["data"] if item["type"] == "tracks"))
        tracks.update((track_id, (isrc, released))
                      for track_id, isrc, released in catalog_dates([page]))

    earliest = {}
    recordings = sorted({isrc for track_id, (isrc, _) in tracks.items()
                         if track_id in saved and isrc})
    print(f"Checking earliest catalog release for {len(recordings)} recordings.", flush=True)
    for index, isrc in enumerate(recordings, 1):
        pages = api.pages("/tracks", countryCode=country, include="albums",
                          **{"filter[isrc]": isrc})
        earliest[isrc] = min((released for _, match, released in catalog_dates(pages)
                              if match == isrc and released is not None), default=None)
        if index % 100 == 0:
            print(f"Checked {index}/{len(recordings)} recordings.", flush=True)

    for track_id in saved:
        isrc, linked_date = tracks.get(track_id, (None, None))
        saved[track_id] = min(filter(None, (linked_date, earliest.get(isrc))), default=None)
    return sorted(saved, key=lambda track_id: (saved[track_id] or date.max, track_id)), saved


def mutate(api, method, path, data, **meta):
    payload = {"data": data}
    if meta:
        payload["meta"] = meta
    headers = {**api.headers, "Idempotency-Key": str(uuid4())}
    return request(API + path, method, headers, json.dumps(payload).encode())


def occurrence(item):
    return {"id": item["id"], "type": item["type"],
            "meta": {"itemId": item["meta"]["itemId"]}}


def sync(api, country, desired):
    owned = api.items("/playlists", **{"filter[owners.id]": "me"})
    matches = [playlist for playlist in owned if attributes(playlist).get("name") == NAME]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple owned playlists are named {NAME!r}. Give them distinct names and rerun.")
    if matches:
        playlist = matches[0]
    else:
        playlist = mutate(api, "POST", "/playlists", {
            "type": "playlists", "attributes": {
                "name": NAME,
                "description": "Saved tracks ordered by earliest TIDAL catalog release date.",
                "accessType": "UNLISTED",
            },
        })["data"]

    playlist_id = quote(playlist["id"], safe="")
    path = f"/playlists/{playlist_id}/relationships/items"
    print(f"https://tidal.com/playlist/{playlist_id}", flush=True)

    def read_items():
        return api.items(path, countryCode=country, sort="itemIndex")

    current = read_items()
    present = {item["id"] for item in current if item["type"] == "tracks"}
    additions = [track_id for track_id in desired if track_id not in present]
    for batch in batches(additions, 50):
        mutate(api, "POST", path, [{"type": "tracks", "id": track_id} for track_id in batch],
               onDuplicates="SKIP")
    if additions:
        current = read_items()

    wanted = set(desired)
    kept = {}
    extras = []
    for item in current:
        track_id = item["id"]
        if item["type"] == "tracks" and track_id in wanted and track_id not in kept:
            kept[track_id] = item
        else:
            extras.append(occurrence(item))
    visible_order = [track_id for track_id in desired if track_id in kept]
    missing = [track_id for track_id in desired if track_id not in kept]

    # Moving the desired suffix to the end preserves the already-correct prefix.
    prefix = 0
    for existing, expected in zip(kept, visible_order):
        if existing != expected:
            break
        prefix += 1
    moves = [occurrence(kept[track_id]) for track_id in visible_order[prefix:]]
    for batch in batches(extras, 50):
        mutate(api, "DELETE", path, batch)
    for batch in batches(moves, 20):
        mutate(api, "PATCH", path, batch)

    final = read_items()
    if [(item["type"], item["id"]) for item in final] != [
        ("tracks", track_id) for track_id in visible_order
    ]:
        raise RuntimeError("Playlist verification failed. Rerun to reconcile the current contents.")
    print(f"Verified {len(final)}/{len(desired)} saved tracks in chronological order.")
    if missing:
        for track_id in missing:
            print(f"  Missing from playlist: https://tidal.com/track/{quote(track_id, safe='')}",
                  file=sys.stderr)
        raise RuntimeError(f"TIDAL did not retain {len(missing)} saved tracks in the playlist.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default=settings()["country"], help="TIDAL catalog country code")
    parser.add_argument("--dry-run", action="store_true", help="Print the ordered track list")
    args = parser.parse_args()
    if args.country and not re.fullmatch(r"[A-Za-z]{2}", args.country):
        parser.error("--country must be a two-letter code")

    scopes = ["collection.read"]
    if not args.dry_run:
        scopes.extend(("playlists.read", "playlists.write"))
    if not args.country:
        scopes.append("user.read")
    api = Tidal(sign_in(" ".join(scopes)))
    country = args.country or next(api.pages("/users/me"))["data"]["attributes"]["country"]
    desired, dates = collection_order(api, country.upper())
    undated = sum(released is None for released in dates.values())
    print(f"Read {len(desired)} saved tracks. {undated} without a release date go last.", flush=True)
    if args.dry_run:
        for track_id in desired:
            print(f"{dates[track_id] or 'Unknown'}  https://tidal.com/track/{quote(track_id, safe='')}")
    else:
        sync(api, country.upper(), desired)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, EOFError, KeyError) as error:
        sys.exit(f"Chronological playlist sync stopped: {error}")
    except KeyboardInterrupt:
        sys.exit(130)
