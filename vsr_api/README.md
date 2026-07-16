# Subtitle Cleanup Service

`vsr_api` wraps this repository's `backend.main.SubtitleRemover` as a single-worker FastAPI service. Its public API is defined in [`../subtitle-cleanup-service-api.md`](../subtitle-cleanup-service-api.md).

## Start

Use the same Python environment that has this repository's VSR dependencies installed:

```powershell
E:\short\venv\vse\python.exe -m pip install -r vsr_api\requirements.txt
E:\short\venv\vse\python.exe -m vsr_api.run
```

The default address is `http://127.0.0.1:8020`; interactive OpenAPI documentation is at `/docs`.

`vsr_api/config.json` controls the listening address and data directory. Set `VSR_API_DATA_ROOT` to override the data directory for a process without changing the file.

## Storage and lifecycle

The service stores its SQLite database, uploaded assets, generated assets, per-job work folders, and task logs under `storage.data_root`. Input and output assets are separate immutable files. Completed output assets can be downloaded only through the output asset ID returned by a successful job, then deleted with `DELETE /api/v1/assets/{asset_id}`.

Tasks execute one at a time in a dedicated child process. This isolates VSR model state and allows a cancellation request to terminate that process and its FFmpeg child-process tree on Windows.
