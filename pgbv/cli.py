"""Command-line entry point.

Examples
--------
    pgbv verify --db shop --out /backups --retention 14
    pgbv verify --db shop --report /var/log/pgbv/last.json --fail-on-mismatch

Connection settings come from flags, or from the standard libpq environment
variables (PGHOST, PGPORT, PGUSER, PGPASSWORD) so the tool fits into any
existing cron / systemd setup without extra configuration.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .core import ConnInfo, verify


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pgbv", description="PostgreSQL backup + restore verification")
    p.add_argument("--version", action="version", version=f"pgbv {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="dump, restore to scratch db, compare, report")
    v.add_argument("--db", required=True, help="database to back up")
    v.add_argument("--host", default=os.getenv("PGHOST", "localhost"))
    v.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")))
    v.add_argument("--user", default=os.getenv("PGUSER", "postgres"))
    v.add_argument("--password", default=os.getenv("PGPASSWORD"),
                   help="or set PGPASSWORD (preferred: never on the command line)")
    v.add_argument("--out", type=Path, default=Path("./backups"), help="dump directory")
    v.add_argument("--report", type=Path, help="write JSON report here")
    v.add_argument("--retention", type=int, help="keep only the N newest dumps")
    v.add_argument("--keep-scratch", action="store_true", help="do not drop the scratch db")
    v.add_argument("--fail-on-mismatch", action="store_true",
                   help="exit 2 when verification fails (for cron / monitoring)")
    v.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    conn = ConnInfo(args.host, args.port, args.user, args.password, args.db)
    report = verify(conn, args.out, keep_scratch=args.keep_scratch, retention=args.retention)

    text = report.to_json()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text)
    else:
        print(text)

    if not report.ok and args.fail_on_mismatch:
        return 2
    return 0 if report.error is None else 1


if __name__ == "__main__":
    sys.exit(main())
