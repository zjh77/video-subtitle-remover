# VSR Worker as a Windows service

Use WinSW for the first deployment. Keep the executable, service XML, private CA, Worker Token and runtime configuration outside this public repository.

## Service account and network boundary

1. Create a dedicated non-interactive Windows account.
2. Grant it read access only to the Worker runtime configuration, token file and CA file.
3. Grant it modify access only to the chosen Worker state directory.
4. Run `vsr_api` separately and configure it to listen on `127.0.0.1` only.
5. Allow outbound HTTPS to the relay task center. Do not create an inbound VSR firewall rule.

## WinSW template

Use an ignored local XML file. Values below are placeholders, not deployable paths or identities.

```xml
<service>
  <id>vsr-relay-worker</id>
  <name>VSR Relay Worker</name>
  <executable>&lt;python-executable&gt;</executable>
  <arguments>-m vsr_worker</arguments>
  <workingdirectory>&lt;repository-directory&gt;</workingdirectory>
  <env name="VSR_RELAY_CONFIG_FILE" value="&lt;protected-config-file&gt;"/>
  <onfailure action="restart" delay="10 sec"/>
  <log mode="roll-by-size">
    <sizeThreshold>10485760</sizeThreshold>
    <keepFiles>7</keepFiles>
  </log>
</service>
```

Install and start with the WinSW executable stored beside this ignored local XML. Verify the local VSR `/health` endpoint before starting the Worker.

## Upgrade and rollback

1. Stop the Worker service; it must not claim a new task while stopping.
2. Wait for the active task to reach a terminal state or allow its lease to expire before replacing code.
3. Install the new version, preserving the protected state directory so unfinished attempts can be recovered.
4. Start the service and verify registration, heartbeat and local VSR health.
5. On rollback, stop the service, restore the previous code version, keep the same state directory and start again. Do not delete the state database during rollback.

## Security checklist

- Worker Token is a high-entropy secret, stored in an ACL-protected file or OS secret store.
- Private CA and token rotation are performed out of band; both old and new trust material may overlap during a planned rotation.
- Never pass Token values on command lines, in service display names or in logs.
- Worker JSONL audit logs are independent from WinSW stdout/stderr rotation.
  Keep the Worker state/log directory ACL-protected and use `job_id` or
  `trace_id` to correlate them with the task-center event stream.
- Nginx IP rules may add rate limiting or block known malicious sources, but must not require a fixed Worker or Workbench egress IP.
