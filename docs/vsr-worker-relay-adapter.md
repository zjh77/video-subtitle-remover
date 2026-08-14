# VSR Worker relay interface adapter

This document describes the contract expected by `vsr_worker/relay_client.py`. It is an implementation checklist for the public task center; all URLs below are relative to its HTTPS origin and contain no deployment values.

## Authentication and transport

- HTTPS is mandatory; the Worker verifies the configured private CA or fixed self-signed certificate.
- Every Worker API request uses `Authorization: Bearer <worker-token>`.
- The server scopes the token to one Worker identity and rejects a mismatched `worker_id`.
- Relay errors must not return secrets in error bodies. The Worker deliberately does not log error bodies.

## Required endpoints

| Worker operation | Method and path | Required response / behavior |
| --- | --- | --- |
| Register | `POST /api/v1/workers/register` | Accept Worker ID and capabilities. Idempotent. |
| Heartbeat | `POST /api/v1/workers/{worker_id}/heartbeat` | May return `cancel_requested`. |
| Claim | `POST /api/v1/workers/{worker_id}/claim` | Long-poll; `204` means no task. |
| Recover attempt | `GET /api/v1/tasks/{task_id}/attempts/{attempt_id}` | Return a current claim only while the same Worker still owns its lease. |
| Renew lease | `POST /api/v1/tasks/{task_id}/attempts/{attempt_id}/lease/renew` | Require lease token; return `lease_valid` and optional cancel flag. |
| Progress / logs | `POST .../progress`, `POST .../logs` | Require lease token; accept idempotent log sequence values. |
| Create output upload | `POST .../output-upload` | Idempotently return an upload ID, part size and already committed parts. |
| Upload part | `PUT /api/v1/uploads/{upload_id}/parts/{part_number}` | Require lease token and return immutable ETag. |
| Complete upload | `POST /api/v1/uploads/{upload_id}/complete` | Validate part list, size and complete SHA-256; return output asset ID. |
| Terminal state | `POST .../complete`, `POST .../fail`, `POST .../cancelled` | Require a valid current lease; completion is idempotent per attempt. |

## Claim response

```json
{
  "task_id": "task-id",
  "attempt_id": "attempt-id",
  "lease_token": "opaque-token",
  "lease_expires_at": "UTC timestamp",
  "input": {
    "asset_id": "asset-id",
    "filename": "input.mp4",
    "size_bytes": 1,
    "sha256": "lowercase SHA-256",
    "download_url": "short-lived HTTPS URL"
  },
  "cleanup": {
    "subtitle_areas": [{"ymin": 0, "ymax": 1, "xmin": 0, "xmax": 1}],
    "inpaint_mode": "sttn-auto"
  }
}
```

The input URL must use the same HTTPS origin as the relay, support `Range`, and provide stable content for the attempt. The Worker never persists the signed URL.

## Transfer integrity and fencing

- Input download: support `Range`, `206`, `Content-Range`, `Content-Length`, and the claim's complete SHA-256.
- Output upload: fixed-size parts with `Content-Range` and `X-Part-SHA256`; server validates the final size and SHA-256.
- A lease-expired, revoked, or superseded attempt must receive `401`, `403`, `409`, `410`, or `412`. The Worker treats these as fenced out and never reports success.
- The server must retain task assets until Workbench confirms its output download, then delete immediately; unconfirmed assets expire after 72 hours. Failed video files expire after 24 hours; task metadata and logs expire after 7 days.
