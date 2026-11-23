"""Audit saved TIDAL tracks and playlist items through the official API."""
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from email.utils import parsedate_to_datetime
import unicodedata
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from tidal_keychain import refresh_token

CONFIG = Path.home() / ".config" / "tidal-scripts" / "config.json"

API = "https://openapi.tidal.com/v2"
COLLECTION = "/userCollectionTracks/me/relationships/items"
PLAYLISTS = "/userCollectionPlaylists/me/relationships/items"
MEDIA_TYPE = "application/vnd.api+json"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"


def token_error_detail(error):
    try:
        payload = json.loads(error.read(8192))
    except (ValueError, UnicodeError):
        return ""
    code = payload.get("error") if isinstance(payload, dict) else None
    hints = {
        "invalid_grant": "",
        "invalid_token": "",
        "invalid_client": "Check the Client ID against the app's Overview tab.",
        "unauthorized_client": "Check that the app is enabled for authorization-code sign-in.",
        "invalid_request": "Check the registered redirect URI against TIDAL_REDIRECT_URI.",
        "invalid_scope": "Check the requested scopes against the app's Allowed scopes.",
        "access_denied": "Check that you approved the requested permissions in TIDAL.",
        "unsupported_grant_type": "TIDAL rejected the authorization-code grant type.",
    }
    if isinstance(code, str) and code in hints:
        return f" ({code})" + (f". {hints[code]}" if hints[code] else "")
    return ""


def diagnose_collection(token):
    api = Tidal(token)
    root = "/userCollectionTracks/me"
    probes = [
        ("Saved tracks resource", root, {}),
        ("Saved tracks item IDs", COLLECTION, {}),
        ("Saved tracks with metadata", COLLECTION, {"include": "items"}),
        ("Saved tracks via resource include", root, {"include": "items"}),
        ("Saved albums (control)", "/userCollectionAlbums/me/relationships/items", {}),
        ("Saved playlists (control)", PLAYLISTS, {}),
    ]
    # One page per route is enough to distinguish access from pagination.
    for label, path, params in probes:
        url = API + path + ("?" + urlencode(params) if params else "")
        try:
            with urlopen(Request(url, headers=api.headers), timeout=15) as response:
                payload = json.load(response)
            data = payload.get("data")
            items = data
            if isinstance(data, dict):
                items = (data.get("relationships") or {}).get("items", {}).get("data")
            summary = f"{len(items)} item references returned" if isinstance(items, list) else (
                "resource returned" if isinstance(data, dict) else "no resource data returned"
            )
            if (payload.get("links") or {}).get("next"):
                summary += ", more pages available"
            print(f"{label}: HTTP {response.status} ({summary})")
        except HTTPError as error:
            with error:
                print(f"{label}: HTTP {error.code}")
        except (URLError, TimeoutError, ValueError):
            print(f"{label}: could not read the response")


def request(url, method="GET", headers=None, body=None):
    for attempt in range(4):
        try:
            with urlopen(Request(url, data=body, headers=headers or {}, method=method),
                         timeout=30) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except HTTPError as error:
            if error.code == 429 and attempt < 3:
                retry_after = (error.headers or {}).get("Retry-After", "").strip()
                delay = 10 * 2 ** attempt
                if retry_after.isdecimal():
                    delay = int(retry_after)
                elif retry_after:
                    try:
                        delay = max(0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass
                print(f"TIDAL rate limit. Retrying {method} in {delay:.0f} seconds...",
                      file=sys.stderr, flush=True)
                error.close()
                time.sleep(delay)
                continue
            if method == "GET" and error.code in (502, 503, 504) and attempt < 3:
                delay = error.headers.get("Retry-After", "")
                time.sleep(min(int(delay), 60) if delay.isdecimal() else 2 ** attempt)
                continue
            detail = token_error_detail(error) if url == TOKEN_URL else ""
            status = f"{error.code} {error.reason or ''}".strip()
            raise RuntimeError(f"{method} {urlsplit(url).path}: HTTP {status}{detail}") from None


def settings():
    try:
        config = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        config = {}
    return {key: os.environ.get("TIDAL_" + key.upper(), config.get(key))
            for key in ("client_id", "redirect_uri", "country")}


def save_settings(config):
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(config, file, indent=2)
            file.close()
            temporary.replace(CONFIG)
        finally:
            temporary.unlink(missing_ok=True)


def callback_code(parts, registered, state):
    values = parse_qs(parts.query)
    if ((parts.scheme, parts.netloc, parts.path) !=
            (registered.scheme, registered.netloc, registered.path)
            or values.get("state") != [state]):
        return None
    if "error" in values:
        raise RuntimeError("TIDAL sign-in was declined or failed.")
    codes = values.get("code", [])
    return codes[0] if len(codes) == 1 else None


def authorization_code(url, redirect, state):
    registered = urlsplit(redirect)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            self.request.settimeout(3)
            super().setup()

        def do_GET(self):
            target = urlsplit(self.path)
            parts = registered._replace(path=target.path, query=target.query)
            try:
                code = callback_code(parts, registered, state)
            except RuntimeError as error:
                result["error"] = error
                code = None
            if code:
                result["code"] = code
            self.send_response(200 if code else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Callback received. Return to Terminal." if code else b"Invalid TIDAL callback.")

    print("Opening TIDAL sign-in. If your browser does not open, use this link:", file=sys.stderr)
    print(url, file=sys.stderr)
    if (registered.scheme == "http" and registered.hostname == "127.0.0.1"
            and registered.port and not registered.query):
        try:
            server = HTTPServer(("127.0.0.1", registered.port), Callback)
        except OSError:
            print("The callback port is busy. Use the manual callback below.", file=sys.stderr)
        else:
            with server:
                webbrowser.open(url)
                server.timeout = 1
                deadline = time.monotonic() + 180
                while not result and time.monotonic() < deadline:
                    server.handle_request()
            if "error" in result:
                raise result["error"]
            if "code" in result:
                return result["code"]
            print("Automatic callback timed out.", file=sys.stderr)
    from getpass import getpass
    parts = urlsplit(getpass("Paste the full redirected URL (input hidden): ").strip())
    code = callback_code(parts, registered, state)
    if not code:
        raise RuntimeError("TIDAL sign-in failed or the redirect could not be verified.")
    return code


def exchange(**fields):
    return request(TOKEN_URL, "POST", {"Content-Type": "application/x-www-form-urlencoded"},
                   urlencode(fields).encode())


def remembered_token(account, value=None):
    try:
        return refresh_token(account, value)
    except RuntimeError as error:
        print(f"{error} Continuing without saving or reusing this login.", file=sys.stderr)
        return None


def sign_in(scopes):
    token = os.environ.get("TIDAL_ACCESS_TOKEN")
    if token:
        return token
    config = settings()
    client, redirect = config["client_id"], config["redirect_uri"]
    if not client or not redirect:
        raise RuntimeError("Set TIDAL_CLIENT_ID and TIDAL_REDIRECT_URI, or TIDAL_ACCESS_TOKEN.")
    account = f"{client}:{' '.join(sorted(scopes.split()))}"
    refresh = remembered_token(account)
    if refresh:
        try:
            tokens = exchange(grant_type="refresh_token", client_id=client, refresh_token=refresh)
        except RuntimeError as error:
            if not any(f"({code})" in str(error) for code in ("invalid_grant", "invalid_token", "access_denied")):
                raise
            print("TIDAL requires a fresh sign-in.", file=sys.stderr)
        else:
            if tokens.get("refresh_token") and tokens["refresh_token"] != refresh:
                remembered_token(account, tokens["refresh_token"])
            return tokens["access_token"]

    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    query = urlencode(dict(response_type="code", client_id=client, redirect_uri=redirect,
                           scope=scopes, code_challenge_method="S256",
                           code_challenge=challenge.decode().rstrip("="), state=state), quote_via=quote)
    code = authorization_code("https://login.tidal.com/authorize?" + query, redirect, state)
    tokens = exchange(grant_type="authorization_code", client_id=client,
                      redirect_uri=redirect, code_verifier=verifier, code=code)
    if tokens.get("refresh_token"):
        remembered_token(account, tokens["refresh_token"])
    return tokens["access_token"]


class Tidal:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": MEDIA_TYPE,
                        "Content-Type": MEDIA_TYPE}

    def pages(self, path, **params):
        url = API + path + ("?" + urlencode(params) if params else "")
        seen = set()
        page_number = 0
        while url:
            parts = urlsplit(url)
            if (parts.scheme, parts.netloc) != ("https", "openapi.tidal.com") or url in seen:
                raise RuntimeError("Unexpected TIDAL pagination link.")
            seen.add(url)
            page_number += 1
            try:
                page = request(url, headers=self.headers)
            except RuntimeError as error:
                raise RuntimeError(f"{error} (page {page_number})") from None
            yield page
            next_link = (page.get("links") or {}).get("next")
            if isinstance(next_link, dict):
                next_link = next_link.get("href")
            if isinstance(next_link, str) and next_link.startswith(path + "?"):
                # TIDAL may return an API-relative path without the /v2 prefix.
                url = API + next_link
            else:
                url = urljoin(url, next_link) if next_link else None

    def items(self, path, **params):
        return [item for page in self.pages(path, **params) for item in page["data"]]

    def tracks(self, country, field, value):
        tracks = []
        for page in self.pages("/tracks", countryCode=country, include="artists,usageRules",
                               **{f"filter[{field}]": value}):
            rules = {item["id"]: attributes(item) for item in page.get("included", [])
                     if item["type"] == "usageRules"}
            for track in page["data"]:
                relationship = (track.get("relationships") or {}).get("usageRules") or {}
                rule = rules.get((relationship.get("data") or {}).get("id"), {})
                if rule.get("countryCode") == country:
                    track["subscription_usage"] = rule.get("subscription")
                tracks.append(track)
        return tracks

    def saved_ids(self):
        return {item["id"] for item in self.items(COLLECTION) if item["type"] == "tracks"}

    def is_saved(self, track_id):
        return any(
            item["type"] == "tracks" and item["id"] == track_id
            for page in self.pages(COLLECTION)
            for item in page["data"]
        )

    def change(self, method, track_id):
        body = json.dumps({"data": [{"type": "tracks", "id": track_id}]}).encode()
        request(API + COLLECTION, method, self.headers, body)


def attributes(track):
    return track.get("attributes") or {}


def streamable(track):
    availability = track.get("subscription_usage")
    if not isinstance(availability, list):
        availability = attributes(track).get("availability")
    return "STREAM" in availability if isinstance(availability, list) else None


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def seconds(value):
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value or "")
    if not match or not any(match.groups()):
        return None
    hours, minutes, duration = (float(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + duration


def artists(track):
    relationship = (track.get("relationships") or {}).get("artists") or {}
    return {artist["id"] for artist in relationship.get("data") or []}


def same_recording(old, new):
    source, candidate = attributes(old), attributes(new)
    isrc = normalize(source.get("isrc"))
    if not isrc or isrc != normalize(candidate.get("isrc")):
        return False
    if not normalize(source.get("title")) or any(
        normalize(source.get(key)) != normalize(candidate.get(key)) for key in ("title", "version")
    ):
        return False
    if not artists(old) or artists(old) != artists(new):
        return False
    if not isinstance(source.get("explicit"), bool) or source["explicit"] != candidate.get("explicit"):
        return False
    a, b = seconds(source.get("duration")), seconds(candidate.get("duration"))
    return a is not None and b is not None and abs(a - b) <= 2


def find_replacement(api, country, old, candidates):
    isrc = normalize(attributes(old).get("isrc"))
    if not isrc:
        return None
    if isrc not in candidates:
        candidates[isrc] = api.tracks(country, "isrc", isrc.upper())
    matches = [track for track in candidates[isrc] if track["id"] != old["id"]
               and streamable(track) is True and same_recording(old, track)]
    return max(matches, key=lambda track: (attributes(track).get("popularity") or 0,
                                           track["id"]), default=None)


def describe(track):
    title = attributes(track).get("title") or "Unknown title"
    version = attributes(track).get("version")
    label = f"{title} ({version})" if version else title
    return f"{label} — https://tidal.com/track/{track['id']}"


def audit(api, country, path, metadata, candidates, include_unknown=False, unknown_ids=None):
    original = {}
    ids = []
    params = {"include": "items.artists" if include_unknown else "items"}
    if path != COLLECTION:
        params.update(countryCode=country, sort="itemIndex")
    print("Reading saved tracks..." if path == COLLECTION else "Reading playlist items...",
          file=sys.stderr, flush=True)
    for position, page in enumerate_items(api.pages(path, **params)):
        linkage, included = page
        original.update(included)
        if linkage["type"] == "tracks":
            ids.append((position, linkage["id"]))
        if position % 200 == 0:
            print(f"  {position:,} entries read so far...", file=sys.stderr, flush=True)
    missing = list(dict.fromkeys(track_id for _, track_id in ids if track_id not in metadata))
    print(f"Read {len(ids):,} track entries. Checking {len(missing):,} IDs in the {country} catalog...",
          file=sys.stderr, flush=True)
    for index in range(0, len(missing), 20):
        batch = missing[index:index + 20]
        fetched = {track["id"]: track for track in api.tracks(country, "id", ",".join(batch))}
        metadata.update({track_id: fetched.get(track_id) for track_id in batch})
        checked = index + len(batch)
        if checked % 200 == 0 or checked == len(missing):
            print(f"  Catalog lookup: {checked:,}/{len(missing):,} IDs checked.",
                  file=sys.stderr, flush=True)
    print("Reviewing availability and looking up same-recording replacements...",
          file=sys.stderr, flush=True)
    unavailable = unknown = 0
    proposals = []
    for position, track_id in ids:
        regional = metadata[track_id]
        status = streamable(regional or {})
        if status is True:
            continue
        old = regional or original.get(track_id) or {"id": track_id}
        if status is None:
            unknown += 1
            if unknown_ids is not None:
                unknown_ids.append(track_id)
            print(f"  {position}. Availability unknown: {describe(old)}", flush=True)
            if not include_unknown:
                continue
            # The collection can supply metadata for IDs absent from the regional catalog.
            saved_track = original.get(track_id)
            if saved_track and not artists(old):
                old = saved_track
        else:
            unavailable += 1
            print(f"  {position}. Unavailable: {describe(old)}", flush=True)
        replacement = find_replacement(api, country, old, candidates)
        if replacement:
            print(f"     Replacement: {describe(replacement)}")
            proposals.append((old, replacement, status))
        else:
            print("     No verified same-recording replacement")
    print(f"{len(ids)} tracks checked, {unavailable} unavailable, {unknown} unknown, "
          f"{len(proposals)} replacement proposals.")
    return proposals


def make_unknown_playlist(api, country, track_ids):
    ids = list(dict.fromkeys(track_ids))
    if not ids:
        print("No unknown tracks to add.")
        return
    playlist = request(API + "/playlists", "POST", api.headers, json.dumps({
        "data": {"type": "playlists", "attributes": {
            "name": "TIDAL availability review",
            "description": "Original saved tracks whose catalog availability could not be confirmed.",
            "accessType": "UNLISTED",
        }},
    }).encode())
    playlist_id = quote(playlist["data"]["id"], safe="")
    path = f"/playlists/{playlist_id}/relationships/items"
    print(f"Created unlisted playlist: https://tidal.com/playlist/{playlist_id}", flush=True)
    skipped = Counter()
    try:
        for index in range(0, len(ids), 50):
            batch = ids[index:index + 50]
            print(f"Submitting unknown originals {index + 1}–{index + len(batch)} of {len(ids)}...",
                  file=sys.stderr, flush=True)
            response = request(API + path, "POST", api.headers, json.dumps({
                "data": [{"type": "tracks", "id": track_id} for track_id in batch],
                "meta": {"onDuplicates": "SKIP"},
            }).encode())
            for item in (response.get("meta") or {}).get("skipped", []):
                skipped[item.get("reason") or "Unspecified"] += 1
    finally:
        for reason, count in sorted(skipped.items()):
            print(f"TIDAL reported {count} skipped playlist items ({reason}).",
                  file=sys.stderr, flush=True)
        # Verify what was stored, including any partial progress after an API error.
        try:
            added = {item["id"] for item in api.items(path, countryCode=country)
                     if item["type"] == "tracks"}
        except (RuntimeError, OSError) as error:
            print(f"Could not verify playlist contents: {error}", file=sys.stderr)
        else:
            missing = [track_id for track_id in ids if track_id not in added]
            print(f"Verified {len(ids) - len(missing)}/{len(ids)} unknown originals in the playlist.")
            for track_id in missing:
                print(f"  Not visible in playlist: https://tidal.com/track/{track_id}")


def enumerate_items(pages):
    position = 0
    for page in pages:
        included = {item["id"]: item for item in page.get("included", [])
                    if item["type"] == "tracks"}
        for linkage in page["data"]:
            position += 1
            yield position, (linkage, included)


def approved_pairs(path):
    pairs = set()
    original = None
    with open(path, encoding="utf-8") as preview:
        for line in preview:
            source = re.fullmatch(
                r"\s*\d+\. (?:Unavailable|Availability unknown): .* — https://tidal\.com/track/(\d+)\s*",
                line,
            )
            replacement = re.fullmatch(r"\s+Replacement: .* — https://tidal\.com/track/(\d+)\s*", line)
            if original and replacement:
                pairs.add((original, replacement[1]))
            original = source[1] if source else None
    if not pairs:
        raise RuntimeError("No replacement pairs found in the approved preview.")
    return pairs


def apply(api, country, proposals, approved=None):
    completed = 0
    if approved is not None:
        proposals = [proposal for proposal in proposals
                     if (proposal[0]["id"], proposal[1]["id"]) in approved]
        print(f"Checking current collection before {len(proposals)} approved swaps...", flush=True)
    saved = api.saved_ids() if approved is not None and proposals else None
    submitted = {}
    for index, (old, replacement, status) in enumerate(proposals, 1):
        old_id, new_id = old["id"], replacement["id"]
        print(f"[{index}/{len(proposals)}] Replace {describe(old)}\n   with {describe(replacement)}",
              flush=True)
        if approved is None:
            confirmation = "yes"
            if status is None:
                print("Original availability was unknown. Check both tracks in signed-in TIDAL.")
                confirmation = "replace"
            if input(f"Type {confirmation} to apply this swap: ").strip().casefold() != confirmation:
                continue
        if approved is None:
            saved = api.saved_ids()
        if old_id not in saved:
            print("Original is no longer saved.")
            continue
        current = {item["id"]: item for item in api.tracks(country, "id", f"{old_id},{new_id}")}
        current_status = streamable(current.get(old_id, {}))
        source = current.get(old_id, old)
        if status is None and current_status is None and not artists(source):
            source = old
        if (current_status is True or (status is False and current_status is not False)
                or streamable(current.get(new_id, {})) is not True
                or not same_recording(source, current[new_id])):
            print("The current catalog no longer verifies this swap.")
            continue
        if new_id not in saved:
            api.change("POST", new_id)
        print("Checking that the replacement is saved...", flush=True)
        if not api.is_saved(new_id):
            raise RuntimeError(f"Could not confirm {new_id} was saved. Original retained.")
        api.change("DELETE", old_id)
        if approved is not None:
            saved.add(new_id)
            saved.discard(old_id)
            submitted[old_id] = new_id
            print("Replacement saved and original removal submitted.", flush=True)
            continue
        if old_id in api.saved_ids():
            raise RuntimeError(f"Could not confirm removal of {old_id}. Replacement was saved.")
        print("Swap verified.")
        completed += 1
    if submitted:
        print(f"Verifying {len(submitted)} swaps against the final collection...", flush=True)
        final = api.saved_ids()
        failed = [(old_id, new_id) for old_id, new_id in submitted.items()
                  if old_id in final or new_id not in final]
        completed = len(submitted) - len(failed)
        for old_id, new_id in failed:
            print(f"Could not verify swap {old_id} → {new_id}.")
    print(f"{completed} swaps verified.")
    if submitted and failed:
        raise RuntimeError("Some swaps could not be verified.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("tracks", "playlists", "diagnose", "configure"))
    parser.add_argument("--playlist", metavar="ID", help="Audit a single playlist instead of saved playlists")
    parser.add_argument("--apply", action="store_true", help="Ask before each saved-track swap")
    parser.add_argument("--approve-preview", metavar="FILE",
                        help="Apply only pairs in an approved scan output without individual prompts")
    parser.add_argument("--include-unknown", action="store_true",
                        help="Look for same-recording replacements for saved tracks with unknown availability")
    parser.add_argument("--unknown-playlist", action="store_true",
                        help="Create an unlisted playlist of unknown originals without swapping tracks")
    parser.add_argument("--country", help="Override the country from your TIDAL account")
    args = parser.parse_args()
    if (args.playlist and args.target != "playlists") or (args.apply and args.target != "tracks"):
        parser.error("--playlist is for playlists and --apply is for tracks")
    if args.include_unknown and args.target != "tracks":
        parser.error("--include-unknown is for tracks")
    if args.unknown_playlist and (args.target != "tracks" or args.apply):
        parser.error("--unknown-playlist is for tracks and cannot be combined with --apply")
    if args.approve_preview and not args.apply:
        parser.error("--approve-preview requires --apply")
    config = settings()
    args.country = args.country or config["country"]
    if args.country and not re.fullmatch(r"[A-Za-z]{2}", args.country):
        parser.error("country must be a two-letter code")
    if args.target == "configure":
        if not config["client_id"] or not config["redirect_uri"]:
            parser.error("set TIDAL_CLIENT_ID and TIDAL_REDIRECT_URI before configuring")
        config["country"] = args.country
        save_settings(config)
        print(f"Saved app settings in {CONFIG}")
        return
    approved = approved_pairs(args.approve_preview) if args.approve_preview else None
    if args.target == "diagnose":
        diagnose_collection(sign_in("collection.read"))
        return
    scopes = []
    if not args.country:
        scopes.append("user.read")
    if args.target == "playlists":
        scopes.append("playlists.read")
    if not args.playlist:
        scopes.append("collection.read")
    if args.apply:
        scopes.append("collection.write")
    if args.unknown_playlist:
        scopes.extend(("playlists.read", "playlists.write"))
    api = Tidal(sign_in(" ".join(scopes)))
    country = args.country or next(api.pages("/users/me"))["data"]["attributes"]["country"]
    if not re.fullmatch(r"[A-Za-z]{2}", country):
        parser.error("country must be a two-letter code")
    country = country.upper()
    print(f"TIDAL catalog region {country}", flush=True)
    if args.target == "tracks":
        print("Swap mode: checking tracks before applying changes..." if args.apply
              else "Playlist mode: collecting tracks with unknown availability..." if args.unknown_playlist
              else "Read-only preview: checking tracks for possible replacements...",
              file=sys.stderr, flush=True)
    metadata, candidates = {}, {}
    if args.target == "tracks":
        unknown_ids = [] if args.unknown_playlist else None
        proposals = audit(api, country, COLLECTION, metadata, candidates, args.include_unknown, unknown_ids)
        if args.unknown_playlist:
            make_unknown_playlist(api, country, unknown_ids)
        if args.apply:
            apply(api, country, proposals, approved)
    else:
        playlists = ([{"id": args.playlist, "type": "playlists"}] if args.playlist
                     else api.items(PLAYLISTS))
        for item in playlists:
            if item["type"] != "playlists":
                continue
            playlist_id = quote(item["id"], safe="")
            print(f"\nPlaylist https://tidal.com/playlist/{playlist_id}")
            audit(api, country, f"/playlists/{playlist_id}/relationships/items",
                  metadata, candidates)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, EOFError, KeyError) as error:
        sys.exit(f"TIDAL audit stopped: {error}")
    except KeyboardInterrupt:
        sys.exit("\nTIDAL audit interrupted.")
