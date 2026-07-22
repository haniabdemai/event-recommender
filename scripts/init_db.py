#!/usr/bin/env python3
"""Create an empty pipeline database with the full schema.

Usage:
    python3 scripts/init_db.py            # creates ./event-recommender.db
    python3 scripts/init_db.py --db PATH  # elsewhere

Idempotent: safe to re-run on an existing DB (adds nothing destructive).
Run scripts/../seed_demo_data.py afterwards for synthetic events to try the
scoring pipeline offline.
"""
import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from erlib import db as erdb  # noqa: E402
from erlib.config import DB_PATH as DEFAULT_DB  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB), type=Path)
    args = ap.parse_args()

    conn = erdb.connect(args.db)
    erdb.init_schema(conn)
    rc = erdb.schema_check(args.db)
    if rc == 0:
        n = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        print(f"Initialised {args.db} ({n} candidates).")
    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
