"""Uvicorn entrypoint."""

from __future__ import annotations

import uvicorn

from querygate.core.config import config as conf
from querygate.core.logging import get_logger


def main() -> None:
    get_logger().info(
        "Starting QueryGate",
        host=conf.host_address,
        port=int(conf.port),
        workers=conf.num_of_workers,
    )
    uvicorn.run(
        "querygate.api.app:app",
        host=conf.host_address,
        port=int(conf.port),
        access_log=True,
        workers=conf.num_of_workers,
        log_config=None,
    )


if __name__ == "__main__":
    main()
