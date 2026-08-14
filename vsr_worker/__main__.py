"""Command-line entry point for the outbound VSR relay Worker."""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, load_settings
from .redact import redact_text
from .runtime import WorkerRuntime


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        runtime = WorkerRuntime(load_settings())
        runtime.run_forever()
        return 0
    except ConfigError as exc:
        logging.getLogger("vsr_worker").error("Configuration error: %s", redact_text(exc))
    except Exception as exc:
        logging.getLogger("vsr_worker").error("Worker startup failed: %s", redact_text(exc))
    return 2


if __name__ == "__main__":
    sys.exit(main())
