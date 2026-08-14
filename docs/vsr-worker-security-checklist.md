# VSR Worker security and recovery checklist

Before enabling a Worker:

- [ ] Relay URL uses HTTPS and the Worker trusts only the intended private CA or self-signed certificate.
- [ ] Service certificate contains the task-center address in its SAN.
- [ ] Worker and Workbench use different scoped high-entropy Tokens.
- [ ] Tokens, CA files, keys, state directories and service XML are outside Git and ACL-protected.
- [ ] VSR API listens only on loopback.
- [ ] Worker state disk has the configured free-space reserve.
- [ ] Task center has 72-hour unconfirmed asset cleanup, 24-hour failed-video cleanup and 7-day metadata/log cleanup.

Expected recovery behavior:

- Downloaded input remains `.part` until size and SHA-256 verify.
- Output multipart progress is durable locally; retries ask the center for already committed parts.
- Worker restart rechecks ownership with the relay before resuming a task.
- Lease loss fences completion. The Worker deletes local VSR assets and media files, retaining only redacted diagnostics for three days.
- Workbench cancellation is received in heartbeat or lease responses and is sent to local VSR immediately.
