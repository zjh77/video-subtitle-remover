# VSR Relay Worker

`vsr_worker` is an outbound, single-concurrency Worker. It claims subtitle-cleanup tasks from a relay task center over HTTPS, then invokes the existing local `vsr_api` service over loopback HTTP.

It does not expose an inbound HTTP endpoint, connect through ZeroTier, or import VSR algorithms directly.

## Runtime configuration

Copy `config.example.json` to an ignored, access-controlled local location, or set `VSR_RELAY_*` variables through the Windows service configuration. Required values are:

- `VSR_RELAY_BASE_URL`: relay HTTPS origin.
- `VSR_RELAY_CA_FILE`: trusted private-CA or self-signed certificate file.
- `VSR_RELAY_WORKER_ID`: stable Worker identity.
- Exactly one of `VSR_RELAY_WORKER_TOKEN` or `VSR_RELAY_WORKER_TOKEN_FILE`.
- `VSR_RELAY_STATE_DIR`: protected local state directory.

`VSR_RELAY_LOCAL_VSR_URL` defaults to `http://127.0.0.1:8020` and rejects non-loopback values.

Start only after the local VSR API is running:

```powershell
python -m vsr_worker
```

The Worker checks its private CA file, token, state disk capacity, and local VSR `/health` before it registers or claims a task.

On restart it first calls the task center's lease-recovery endpoint for every
non-terminal SQLite record. A valid lease resumes its persisted local VSR job;
the fresh response supplies a replacement input download URL. A rejected lease
is fenced, its local media is removed, and the Worker never creates another
VSR job for that attempt. A `cancel_requested` recovery response cancels the
persisted local job before reporting it cancelled.

## Security rules

- Do not put runtime configuration, CA files, private keys, tokens, task-center addresses, signed URLs, or videos in this repository.
- Do not disable TLS verification.
- The Worker redacts Authorization values, signed URL queries, and Windows paths before forwarding task logs or writing diagnostics.
- A lost lease fences the Worker from reporting task success.
