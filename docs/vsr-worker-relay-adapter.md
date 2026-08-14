# VSR Worker lease adapter

The Worker uses the formal API only: worker register, heartbeat, `leases:claim`, then `/worker-leases/{lease_id}/renew`, progress, logs, output-upload, complete, fail, or cancelled.

Claim `operation.options.subtitle_areas` and `inpaint_mode` are strictly validated and passed unchanged to local VSR. Input artifact URLs may be relative or absolute HTTPS URLs on the relay origin.

Output is `output-upload`, `GET /uploads/{upload_id}`, zero-based part PUTs with `Content-Range` and `X-Part-SHA256`, `/uploads/{upload_id}/complete`, then lease complete with `output_sha256`. A 409 is a fenced lease and must never produce completion.
