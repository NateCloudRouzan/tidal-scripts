# TIDAL collection repair

A Python 3 command-line tool for finding unavailable saved tracks, proposing playable versions of the same recording, and auditing playlists. It uses the [official TIDAL Web API](https://tidal-music.github.io/tidal-api-reference/).

## Connect TIDAL

Create an app in the [TIDAL Developer Dashboard](https://developer.tidal.com/dashboard) and register a redirect URI. Use that exact URI below. For a local app, if accepted by the dashboard, use a loopback URL such as `http://127.0.0.1:8765/callback`. The script uses OAuth with PKCE. Access tokens stay in memory and refresh tokens are stored in macOS Keychain, by Client ID and permission set.

```sh
export TIDAL_CLIENT_ID='your app client ID'
export TIDAL_REDIRECT_URI='http://127.0.0.1:8765/callback'
python3 tidal_audit.py configure --country US
python3 tidal_audit.py tracks
```

Use your TIDAL account's country in the configure command. It stores the Client ID, redirect URI, and country in `~/.config/tidal-scripts/config.json`, outside this repository. Environment variables override the saved values. Once configured, you can run commands from a fresh terminal without exporting them again.

On the first run, the script opens your browser and receives the callback automatically for the registered `127.0.0.1` URI. If the port is busy or you're using a different kind of redirect, open the printed link and paste the full address into the hidden prompt. The manual loopback redirect can show a connection error while still leaving the callback address in the browser.

On later runs, the script exchanges the Keychain refresh token for a new access token. A new permission set, such as the first run with `--apply`, can require a separate browser authorization. You may also need to authorize again if TIDAL rejects an expired or revoked refresh token. macOS may ask you to allow Python to access the Keychain item. For a one-off run, an existing user access token can be supplied via `TIDAL_ACCESS_TOKEN`.

Keep credentials, callback URLs, and scan output out of GitHub. The repository ignores local config and environment files. The tool does not need a client secret for this flow.

The collection operations require `collection.read` and, for swaps, `collection.write`. TIDAL controls which scopes your developer app can request. An HTTP 403 is an access failure. Use `--country US` (substituting your account country) to skip the profile lookup when needed.

## Commands

```sh
python3 tidal_audit.py tracks
python3 tidal_audit.py tracks --apply
python3 tidal_audit.py playlists
python3 tidal_audit.py playlists --playlist PLAYLIST_ID
```

If My Collection → Tracks returns HTTP 404, run `python3 tidal_audit.py diagnose`.
Sign in with the fresh link. This uses only `collection.read` and checks the
documented saved-track resource and item reads, plus album and playlist controls.
It prints status codes and first-page counts without printing library entries or
credentials. If a control succeeds while all track reads return 404, the failure
is specific to the track routes tested. A 404 alone does not identify the access
tier or rule responsible.

The playlist audit reports each affected position, including duplicate occurrences. Paste the ID from a TIDAL playlist link to audit a single playlist.

Matches require the same ISRC, title, version, artist IDs, explicit flag, and duration within two seconds. When multiple tracks qualify, the tool proposes the most popular one returned by TIDAL. Each proposed swap requires a separate confirmation. The script adds and verifies the replacement before removing the original. Saving a different track can change its position in the collection's date-added sort.

Availability comes from the regional catalog's subscription usage rules, with the legacy `availability` field as a fallback. A missing catalog result or availability field is reported as unknown. This check reflects catalog streaming eligibility.

Use `python3 tidal_audit.py tracks --country US --include-unknown` to look for replacements for unknown tracks too. The tool uses collection metadata when available and still requires a verified same-recording match and a streamable replacement. Add `--apply` to review swaps. For an unknown original, check both versions in signed-in TIDAL and type `replace` only if you want the swap.

## Review tracks with unknown availability

Run `python3 tidal_audit.py tracks --unknown-playlist` to create an unlisted
playlist of the original tracks still marked unknown in My Collection. This
requires `playlists.read` and `playlists.write` and may open a new authorization
prompt. It does not swap saved tracks. Open the printed playlist in signed-in
TIDAL to test playback. TIDAL may omit a track from the playlist if its catalog
entry is inaccessible. The script reports which IDs were not visible when it
verified the playlist.

When TIDAL rate-limits a request, the script follows Retry-After when supplied
and retries up to three times. If a swap batch stops, rerun the same approved
preview after the cooldown. The new run reads the current collection and checks
that a replacement is saved before removing its original.
