from __future__ import annotations

import uvicorn

from vsr_api.app.config import SERVER_HOST, SERVER_PORT, write_default_config
from vsr_api.app.service_logging import configure_api_logging, log_exception


def main() -> None:
    write_default_config()
    configure_api_logging()
    try:
        uvicorn.run("vsr_api.app.main:app", host=SERVER_HOST, port=SERVER_PORT)
    except Exception:
        log_exception("api_process_unhandled_exception")
        raise


if __name__ == "__main__":
    main()
