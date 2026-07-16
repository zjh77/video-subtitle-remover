from __future__ import annotations

import uvicorn

from vsr_api.app.config import SERVER_HOST, SERVER_PORT, write_default_config


def main() -> None:
    write_default_config()
    uvicorn.run("vsr_api.app.main:app", host=SERVER_HOST, port=SERVER_PORT)


if __name__ == "__main__":
    main()
