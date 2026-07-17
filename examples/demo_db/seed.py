"""Create and seed the local SQLite demo database.

Usage: python examples/demo_db/seed.py [path/to/db.sqlite3]

For a Postgres demo (matching docker-compose.yml + connections.example.yaml),
the same schema/data loads automatically on first container start via
examples/demo_db/init_postgres.sql — no separate step needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.demo_db.schema import create_and_seed  # noqa: E402

DEFAULT_PATH = Path(__file__).resolve().parent / "querygate_demo.db"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    engine = sa.create_engine(f"sqlite:///{path}")
    create_and_seed(engine)
    print(f"Seeded demo database at {path}")


if __name__ == "__main__":
    main()
