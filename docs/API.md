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

If the replay already exists on the Switch, it is returned immediately via FTP without interacting with the game (~2s). Otherwise the state machine types the code into the game, waits for the download, then retrieves the file via FTP (~30s).

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
| 200 | `application/octet-stream` | Raw `.rpl.zs` bytes | Replay fetched successfully (new or duplicate) |
| 400 | text | `Invalid replay code: ...` | Code missing or invalid format (16 alphanumeric, starts with R) |
| 401 | text | `Unauthorized` | Missing or invalid Bearer token |
| 404 | text | `Not found` | Invalid endpoint |
| 500 | text | Error message | Game fetch error, FTP failure, or state machine error |

#### Example

```bash
curl -X POST http://localhost:5003/replay \
  -H "Authorization: Bearer <api_secret>" \
  -H "Content-Type: application/json" \
  -d '{"code": "RQ22FYSN00000000"}' \
  --output replay.rpl.zs
```

## Notes

- Requests are processed sequentially. If multiple requests are queued, each waits for the previous one to complete.
- The connection stays open until the replay is fully processed. Expect beyond 30s for new replays, ~2s for duplicates.
- The returned file is a Zstandard-compressed replay (`.rpl.zs`).
