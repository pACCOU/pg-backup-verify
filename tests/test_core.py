"""Tests. The unit tests run anywhere; the integration test needs a reachable
PostgreSQL (set PGBV_TEST_DSN, e.g. "host=localhost port=5432 user=postgres")
and is skipped otherwise."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from pgbv.core import ConnInfo, TableStat, admin_exec, compare, prune, verify


# ----------------------------------------------------------------- unit ---- #

def test_compare_identical():
    a = [TableStat("public", "users", 10), TableStat("public", "orders", 3)]
    assert compare(a, list(a)) == []


def test_compare_detects_missing_and_count_diff():
    src = [TableStat("public", "users", 10), TableStat("public", "orders", 3)]
    dst = [TableStat("public", "users", 9)]
    diffs = compare(src, dst)
    assert {d["table"]: d["reason"] for d in diffs} == {
        "public.orders": "missing in restore",
        "public.users": "row count differs",
    }


def test_prune_keeps_newest():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        files = []
        for i in range(5):
            f = out / f"shop-2026010{i}-000000.dump"
            f.write_bytes(b"x")
            os.utime(f, (1000 + i, 1000 + i))
            files.append(f)
        removed = prune(out, "shop", keep=2)
        assert sorted(p.name for p in removed) == [files[0].name, files[1].name, files[2].name]
        assert sorted(p.name for p in out.glob("*.dump")) == [files[3].name, files[4].name]


# ---------------------------------------------------------- integration ---- #

DSN = os.getenv("PGBV_TEST_DSN")


def _conn_from_dsn(dsn: str, dbname: str) -> ConnInfo:
    parts = dict(p.split("=", 1) for p in dsn.split())
    return ConnInfo(
        host=parts.get("host", "localhost"),
        port=int(parts.get("port", 5432)),
        user=parts.get("user", "postgres"),
        password=parts.get("password"),
        dbname=dbname,
    )


@pytest.mark.skipif(not DSN, reason="PGBV_TEST_DSN not set")
def test_full_cycle_roundtrip():
    from psycopg2 import sql

    dbname = "pgbv_it_source"
    admin = _conn_from_dsn(DSN, "postgres")
    admin_exec(
        admin,
        sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)),
        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)),
    )
    src = admin.with_db(dbname)
    c = src.connect()
    with c.cursor() as cur:
        cur.execute("CREATE SCHEMA app")
        cur.execute("CREATE TABLE app.customers(id serial primary key, name text, email text)")
        cur.execute("CREATE TABLE app.orders(id serial primary key, customer_id int references app.customers, total numeric)")
        cur.execute("INSERT INTO app.customers(name,email) SELECT 'c'||g, 'c'||g||'@x.io' FROM generate_series(1,250) g")
        cur.execute("INSERT INTO app.orders(customer_id,total) SELECT (g % 250)+1, g*1.5 FROM generate_series(1,1000) g")
    c.commit()
    c.close()

    with tempfile.TemporaryDirectory() as d:
        report = verify(src, Path(d), retention=3)
        assert report.error is None, report.error
        assert report.ok
        assert report.tables_source == 2 and report.tables_restored == 2
        assert report.rows_source == 1250 and report.rows_restored == 1250
        assert Path(report.dump_file).exists()

    admin_exec(admin, sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))
