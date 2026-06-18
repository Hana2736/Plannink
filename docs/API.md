# Plannink Replay Fetch API

## Base URL

```
http://<host>:<port>
```

Default port is `5003`, configurable in `config.json` under `server.api_port`.

## Authentication

All requests require a Bearer token in the `Authorization` header.

```
Authorization: Bearer <api_secret>
```

The secret is configured in `config.json` under `api_secret`.

## Endpoints

### POST /replay

Fetch a Splatoon 3 replay file by its replay code. The request is queued and processed sequentially by the state machine — the connection blocks until the replay is fetched or an error occurs.

The code is handed to the **gem worker** running inside the game on the Switch (over the gem socket), which drives the game's own replay worker and streams the file back. The GUI/state machine only walks the player to the lobby terminal and keeps the game alive there; codes are no longer typed in, there is no terminal-menu navigation, and there is no FTP step. Codes are submitted strictly one at a time — the next is not sent to the worker until the current one has produced a response or failed.

#### Request

**Headers:**
| Header | Required | Value |
|---|---|---|
| `Authorization` | Yes | `Bearer <api_secret>` |
| `Content-Type` | No | `application/json` |

**Body (JSON):**
```json
{
  "code": "RQ22FYSN00000000"
}
```

Alternatively, the code can be sent as a plain text body.

#### Responses

| Status | Content-Type | Body | Description |
|---|---|---|---|
| 200 | `application/octet-stream` | Raw replay bytes | Replay fetched successfully |
| 400 | text | `Invalid replay code: ...` | Code missing or invalid format (16 alphanumeric, starts with R) |
| 401 | text | `Unauthorized` | Missing or invalid Bearer token |
| 404 | text | `Bad replay code: replay not found` | Gem worker rejected the code — invalid / nonexistent replay (gem `BadReplayCode`). Also returned for the unknown `/endpoint` case with body `Not found`. |
| 500 | text | Error message | Generic worker failure (gem `ReplayDownloadFailure`), console not connected, gem worker timeout, or state-machine timeout. The body text distinguishes the case. |

#### Example

```bash
curl -X POST http://localhost:5003/replay \
  -H "Authorization: Bearer <api_secret>" \
  -H "Content-Type: application/json" \
  -d '{"code": "RQ22FYSN00000000"}' \
  --output replay.rpl.zs
```

## Notes

- Requests are processed sequentially. If multiple requests are queued, each waits for the previous one to complete — exactly one code is ever in flight to the gem worker (sending a second while it is busy would crash the game).
- The connection stays open until the replay is fully processed.
- The returned bytes are the raw replay file as the game's replay worker produces it.
- 404 (`Bad replay code: replay not found`) means the code itself is bad/nonexistent; a 500 means the worker, link, or state machine failed and the code may be worth retrying.

## Gem socket

The gem-injected game on the Switch is a TCP client that connects *out* to Plannink. Plannink listens on `gem.bind:gem.port` (default `0.0.0.0:6388`, also overridable via `PLANNINK_GEM_BIND` / `PLANNINK_GEM_PORT`). Point the console's `sd:/gem/config.txt` `server=`/`port=` at this host.

Two kinds of upstream notification get forwarded to the main app — both POST with `Authorization: Bearer <pool_ingest.token>` (same token covers both). The token is set in `config.json` under `pool_ingest.token` or via the `PLANNINK_POOL_INGEST_TOKEN` env var; if unset, the corresponding payloads are dropped with a warning. Both forwarders are best-effort and never affect replay serving.

- **`UploadReplayNotification`** → POST `pool_ingest.url` (default `https://hana.lol/inksight/pool_ingest_code`) with body `{"code": "R..."}`. The NSA ID and NPLN ID present in the wire packet are *not* forwarded on this endpoint.
- **`FriendPlayingNotification`** → POST `pool_ingest.player_playing_update_url` (default `https://hana.lol/inksight/player_playing_update`) with body:
  ```json
  {
    "timestamp":  1748390000,
    "nsa_id":     "0462d0667a79aaa1",
    "subtype":    "StartSolo",
    "match_mode": 4,
    "sender":     "u-apcykoaq5r2xbviomnmm"
  }
  ```
  `subtype` is one of `StartSolo` / `CreateRoom` / `JoinRoom`; `nsa_id` is the sender's NSA ID as a 16-char zero-padded lowercase hex string; `sender` is the NPLN ID.

## Health heartbeat

A background thread POSTs the bot's readiness to `pool_ingest.cloudfetch_health_url` (default `https://hana.lol/inksight/cloudfetch_health`) every 60 seconds, using the same `Authorization: Bearer <pool_ingest.token>`. Body is `{"state": "ready"}` or `{"state": "loading"}`. The main app flags the fetcher **crashed** if it sees no successful ping for >90s.

What gets reported each tick is derived from the gem link and the current vision state:

| Condition | Reported |
|---|---|
| Parked at the lobby terminal (`LobbyVersus_LobbyAtTml`), gem connected | `ready` |
| Booting / navigating to the lobby (`BootSplash`, `LoadingScreen`, title, news, freeroam, lobby nav, …) | `loading` |
| Gem not connected | *(no ping)* |
| Error popup (`OSErr`, `SystemWindow`) or Switch HOME menu (`HOMEMenu*`) | *(no ping)* |

The "no ping" cases are intentional silence: if the bot can't serve, we let the main app's >90s timeout surface it as down rather than reporting a misleading state. The ping is best-effort and never affects replay serving. If `pool_ingest.token` is unset, the heartbeat is disabled (logged once at startup).
