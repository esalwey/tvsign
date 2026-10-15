"""Entry point: `python -m app` (Docker, Windows desktop launcher, manual runs).

Runs uvicorn in-process with a single worker and no reloader. On Windows this keeps the default
Proactor event loop, which is required for asyncio subprocesses (plumesign); uvicorn switches to
the Selector loop when it has to spawn its own worker processes.
Listens on TVSIGN_HOST:TVSIGN_PORT (127.0.0.1:8765 on Windows, 0.0.0.0:8765 elsewhere).
"""
import asyncio
import os

import uvicorn

from .config import HOST, PORT


def main() -> None:
    proxies = os.environ.get("TVSIGN_TRUSTED_PROXIES", "127.0.0.1")
    config = uvicorn.Config(
        "app.main:app", host=HOST, port=PORT, reload=False, workers=1, loop="asyncio",
        proxy_headers=True, forwarded_allow_ips=proxies, server_header=False,
        log_level=os.environ.get("TVSIGN_LOG_LEVEL", "info"),
        # the page polls /api/state every 5 s: one access line each would fill the Docker log
        access_log=os.environ.get("TVSIGN_ACCESS_LOG", "").strip().lower() in ("1", "true", "yes", "on"),
    )
    server = uvicorn.Server(config)
    # asyncio.run keeps the platform default loop (Proactor on Windows)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
