import logging
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from vsr_api.app.service_logging import LOGGER_NAME, configure_api_logging, log_event


class ApiLoggingTests(unittest.TestCase):
    def test_service_log_redacts_and_includes_job_id(self):
        root = Path.cwd() / "tests" / f".runtime-test-state-{uuid4().hex}"
        try:
            logger = configure_api_logging(root)
            log_event(logging.ERROR, "local_job_runner_error", job_id="subclean_test", error="Bearer secret-token path C:\\private\\video.mp4 https://host/file?signature=secret")
            for handler in logger.handlers:
                handler.flush()
            text = (root / "vsr-api.log").read_text(encoding="utf-8")
            self.assertIn("job_id=subclean_test", text)
            self.assertIn("[REDACTED]", text)
            self.assertIn("[LOCAL_PATH]", text)
            self.assertNotIn("secret-token", text)
            self.assertNotIn("signature=secret", text)
        finally:
            logger = logging.getLogger(LOGGER_NAME)
            for handler in list(logger.handlers):
                handler.close(); logger.removeHandler(handler)
            shutil.rmtree(root, ignore_errors=True)
