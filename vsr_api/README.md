# Subtitle Cleanup Service

`vsr_api` wraps this repository's `backend.main.SubtitleRemover` as a single-worker FastAPI service. Its public API is defined in [`../subtitle-cleanup-service-api.md`](../subtitle-cleanup-service-api.md).

## Start

Use a Python environment that has this repository's VSR dependencies installed:

```powershell
python -m pip install -r vsr_api\requirements.txt
python -m vsr_api.run
```

On Windows, `scripts\start-vsr-api.bat` starts the same service from the
project root using the Conda environment at `D:\conda_envs\vsr`; pre-set `VSR_API_HOST`,
`VSR_API_PORT`, or `VSR_API_DATA_ROOT` only when overriding its safe defaults
(including the default data root `D:\short\vsr-data`).

For relay deployments, `scripts\start-vsr-relay.bat` starts or reuses the
loopback VSR API, waits for `/health`, then starts the Worker using the ignored
`private\vsr-worker.json` configuration file.

The default address is `http://127.0.0.1:8020`; interactive OpenAPI documentation is at `/docs`. Do not expose this service publicly. The relay Worker accesses it only through localhost.

Copy `vsr_api/config.example.json` to the ignored local file `vsr_api/config.json` only when file-based configuration is required. `VSR_API_HOST`, `VSR_API_PORT`, and `VSR_API_DATA_ROOT` override it. Do not place real deployment paths or credentials in this repository.

## Storage and lifecycle

The service stores its SQLite database, uploaded assets, generated assets, per-job work folders, and task logs under `storage.data_root`. Input and output assets are separate immutable files. Completed output assets can be downloaded only through the output asset ID returned by a successful job, then deleted with `DELETE /api/v1/assets/{asset_id}`.

Tasks execute one at a time in a dedicated child process. This isolates VSR model state and allows a cancellation request to terminate that process and its FFmpeg child-process tree on Windows.
