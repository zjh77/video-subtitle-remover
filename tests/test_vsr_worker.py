from __future__ import annotations

import hashlib
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from vsr_worker.models import ClaimedTask, UploadSession
from vsr_worker.redact import redact_text
from vsr_worker.state import WorkerState
from vsr_worker.transfer import download_with_resume, upload_output_with_resume


def claim() -> ClaimedTask:
    return ClaimedTask.from_response({"task_id": "task", "attempt_id": "attempt", "lease_token": "lease", "lease_expires_at": "future", "input": {"asset_id": "input", "filename": "input.mp4", "size_bytes": 1, "sha256": "a" * 64, "download_url": "https://relay.invalid/download?signature=hidden"}, "cleanup": {"subtitle_areas": [{"ymin": 0, "ymax": 1, "xmin": 0, "xmax": 1}], "inpaint_mode": "opencv"}})


class Response:
    def __init__(self, data: bytes, start: int):
        self.status = 206 if start else 200
        self._stream = BytesIO(data[start:])
        self._start = start
        self._total = len(data)

    def header(self, key: str):
        if key == "Content-Range" and self._start:
            return f"bytes {self._start}-{self._total - 1}/{self._total}"
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        pass


class FakeRelay:
    def __init__(self):
        self.parts: dict[int, str] = {}

    def create_output_upload(self, _claim, _size, _sha, _metadata):
        return UploadSession("upload", 3, self.parts.copy())

    def upload_part(self, _claim, _upload, number, _range, sha, _content):
        etag = f"etag-{number}"
        self.parts[number] = etag
        return etag

    def complete_output_upload(self, _claim, _upload, parts, size, sha):
        assert size > 0 and len(sha) == 64 and parts
        return "output"


class WorkerTransferTests(unittest.TestCase):
    def test_redaction_removes_bearer_url_query_and_windows_path(self):
        result = redact_text("Bearer secret https://relay.invalid/a?sig=secret C:\\private\\file")
        self.assertNotIn("secret", result)
        self.assertNotIn("private", result)

    def test_range_download_resumes_and_checks_hash(self):
        data = b"abc" * 100
        with TemporaryDirectory() as directory:
            target = Path(directory) / "input.mp4"
            target.with_suffix(".mp4.part").write_bytes(data[:7])
            download_with_resume(target, len(data), hashlib.sha256(data).hexdigest(), lambda start: Response(data, start))
            self.assertEqual(target.read_bytes(), data)

    def test_output_upload_skips_server_committed_parts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = WorkerState(root); state.initialize(); current = claim(); state.save_claim(current)
            output = root / "output.mp4"; output.write_bytes(b"abcdefgh")
            relay = FakeRelay(); relay.parts[1] = "etag-1"
            asset_id = upload_output_with_resume(relay, current, state, output, {"width": 1})
            self.assertEqual(asset_id, "output")
            self.assertEqual([item["part_number"] for item in state.upload_parts("task", "attempt")], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
