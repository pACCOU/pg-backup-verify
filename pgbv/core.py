"""Core logic: dump, restore into a scratch database, compare, report.

Design notes
------------
* Uses the official ``pg_dump`` / ``pg_restore`` binaries (custom format) so
  the backups are the same ones you would use in production.
* The restore target is a throw-away database created for the run and
  dropped afterwards, so a verification never touches the source.
* Verification compares the set of tables and the row count of each table
  between source and restored copy. That catches truncated dumps, missing
  schemas, and silently failing restores — the three failure modes that
  bite people in practice.
* Every step is logged; the final report is JSON so it can be shipped to a
  monitoring system or attached to an alert e-mail.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import psycopg2
from psycopg2 import sql

log = logging.getLogger("pgbv")


@dataclass
class ConnInfo:
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str | None = None
    dbname: str = "postgres"

    def env(self) -> dict[str, str]:
        """Environment for pg_dump / pg_restore (PGPASSWORD, never argv)."""
        env = os.environ.copy()
        if self.password:
            env["PGPASSWORD"] = self.password
        return env

    def args(self) -> list[str]:
        return ["-h", self.host, "-p", str(self.port), "-U", self.user]

    def connect(self, dbname: str | None = None):
        return psycopg2.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=dbname or self.dbname,
        )

    def with_db(self, dbname: str) -> "ConnInfo":
        return ConnInfo(self.host, self.port, self.user, self.password, dbname)


@dataclass
class TableStat:
    schema: str
    name: str
    rows: int


@dataclass
class Report:
    source_db: str
    dump_file: str
    started_at: str
    finished_at: str = ""
    dump_seconds: float = 0.0
    restore_seconds: float = 0.0
    dump_bytes: int = 0
    tables_source: int = 0
    tables_restored: int = 0
    rows_source: int = 0
    rows_restored: int = 0
    mismatches: list[dict] = field(default_factory=list)
    ok: bool = False
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise RuntimeError(f"{binary} not found on PATH")
    return path


def _run(cmd: list[str], env: dict[str, str]) -> None:
    log.debug("exec: %s", " ".join(cmd))
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {proc.stderr.strip()}")
    if proc.stderr.strip():
        log.debug("%s stderr: %s", cmd[0], proc.stderr.strip())


def table_stats(conn: ConnInfo, dbname: str) -> list[TableStat]:
    """Exact row counts for every user table (slow on huge DBs, but exact)."""
    out: list[TableStat] = []
    c = conn.connect(dbname)
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY 1, 2
                """
            )
            tables = cur.fetchall()
            for schema, name in tables:
                cur.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(name)
                    )
                )
                out.append(TableStat(schema, name, cur.fetchone()[0]))
    finally:
        c.close()  # psycopg2's "with" commits but does not close
    return out


def compare(src: Iterable[TableStat], dst: Iterable[TableStat]) -> list[dict]:
    """Return the list of differences between two table-stat sets."""
    a = {(t.schema, t.name): t.rows for t in src}
    b = {(t.schema, t.name): t.rows for t in dst}
    diffs: list[dict] = []
    for key in sorted(set(a) | set(b)):
        ra, rb = a.get(key), b.get(key)
        if ra != rb:
            diffs.append(
                {
                    "table": f"{key[0]}.{key[1]}",
                    "source_rows": ra,
                    "restored_rows": rb,
                    "reason": "missing in restore" if rb is None
                    else "missing in source" if ra is None
                    else "row count differs",
                }
            )
    return diffs


# --------------------------------------------------------------------------- #
# main operations
# --------------------------------------------------------------------------- #

def dump(conn: ConnInfo, out_dir: Path) -> tuple[Path, float]:
    """pg_dump in custom format. Returns (path, seconds)."""
    pg_dump = _require("pg_dump")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"{conn.dbname}-{stamp}.dump"
    t0 = time.perf_counter()
    _run([pg_dump, *conn.args(), "-Fc", "-f", str(target), conn.dbname], conn.env())
    return target, time.perf_counter() - t0


def admin_exec(conn: ConnInfo, *statements) -> None:
    """Run statements that must be outside a transaction (CREATE/DROP DATABASE)."""
    c = conn.with_db("postgres").connect()
    try:
        c.autocommit = True
        with c.cursor() as cur:
            for st in statements:
                cur.execute(st)
    finally:
        c.close()


def restore_to_scratch(conn: ConnInfo, dump_file: Path, scratch_db: str) -> float:
    """Create scratch DB, pg_restore into it. Returns seconds."""
    pg_restore = _require("pg_restore")
    admin_exec(
        conn,
        sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(scratch_db)),
        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch_db)),
    )
    t0 = time.perf_counter()
    _run(
        [pg_restore, *conn.args(), "--no-owner", "--no-privileges",
         "-d", scratch_db, str(dump_file)],
        conn.env(),
    )
    return time.perf_counter() - t0


def drop_db(conn: ConnInfo, dbname: str) -> None:
    admin_exec(conn, sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))


def verify(conn: ConnInfo, out_dir: Path, keep_scratch: bool = False,
           retention: int | None = None) -> Report:
    """Full cycle: dump -> restore -> compare -> report."""
    report = Report(
        source_db=conn.dbname,
        dump_file="",
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    scratch = f"{conn.dbname}_verify_{int(time.time())}"
    try:
        log.info("dumping %s", conn.dbname)
        dump_file, report.dump_seconds = dump(conn, out_dir)
        report.dump_file = str(dump_file)
        report.dump_bytes = dump_file.stat().st_size
        log.info("dump done: %s (%.1f KB, %.1fs)", dump_file.name,
                 report.dump_bytes / 1024, report.dump_seconds)

        log.info("restoring into scratch db %s", scratch)
        report.restore_seconds = restore_to_scratch(conn, dump_file, scratch)
        log.info("restore done (%.1fs)", report.restore_seconds)

        src = table_stats(conn, conn.dbname)
        dst = table_stats(conn, scratch)
        report.tables_source, report.tables_restored = len(src), len(dst)
        report.rows_source = sum(t.rows for t in src)
        report.rows_restored = sum(t.rows for t in dst)
        report.mismatches = compare(src, dst)
        report.ok = not report.mismatches and report.tables_source > 0
        if report.ok:
            log.info("OK: %d tables, %d rows match", len(src), report.rows_source)
        else:
            log.error("MISMATCH: %s", json.dumps(report.mismatches))

        if retention:
            prune(out_dir, conn.dbname, retention)
    except Exception as exc:  # noqa: BLE001 - we want everything in the report
        report.ok = False
        report.error = str(exc)
        log.exception("verification failed")
    finally:
        if not keep_scratch:
            try:
                drop_db(conn, scratch)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not drop scratch db %s: %s", scratch, exc)
        report.finished_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return report


def prune(out_dir: Path, dbname: str, keep: int) -> list[Path]:
    """Keep the newest ``keep`` dumps for ``dbname``; delete the rest."""
    files = sorted(out_dir.glob(f"{dbname}-*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in files[keep:]:
        old.unlink()
        removed.append(old)
        log.info("pruned old dump %s", old.name)
    return removed
