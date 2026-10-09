# pg-backup-verify

**A backup you have never restored is not a backup.**
`pgbv` dumps a PostgreSQL database, restores the dump into a throw-away
database on the same server, compares every table's row count with the
source, and writes a JSON report. Run it from cron or a container and wire
the exit code into your monitoring — you will know within minutes if a
backup silently stopped being restorable.

```
$ pgbv verify --db shop --out /backups --retention 14 --fail-on-mismatch
2026-10-07 09:00:01 INFO    dumping shop
2026-10-07 09:00:03 INFO    dump done: shop-20261007-090001.dump (184.2 KB, 1.9s)
2026-10-07 09:00:03 INFO    restoring into scratch db shop_verify_1791428401
2026-10-07 09:00:05 INFO    restore done (2.1s)
2026-10-07 09:00:05 INFO    OK: 12 tables, 48 312 rows match
```

## What it checks

| Failure mode                              | Caught by                              |
|-------------------------------------------|----------------------------------------|
| Dump truncated / disk full / wrong creds  | `pg_dump` exit code + restore failure  |
| Schema missing from dump (`-n` mistakes)  | table set comparison                   |
| Restore "succeeds" but data is missing    | exact per-table row count comparison   |
| Backups growing stale on disk             | `--retention N` pruning                |

## Install

```bash
pip install .            # needs pg_dump / pg_restore on PATH (postgresql-client)
# or
docker build -t pgbv . && docker run --rm -e PGPASSWORD=... -v /backups:/backups \
  pgbv verify --db shop --host db.internal --out /backups
```

## Usage

```
pgbv verify --db NAME [--host H] [--port P] [--user U]
            [--out DIR] [--report FILE.json] [--retention N]
            [--keep-scratch] [--fail-on-mismatch] [-v]
```

Credentials are read from the standard libpq variables (`PGHOST`, `PGPORT`,
`PGUSER`, `PGPASSWORD`) so nothing secret has to appear on the command line.

Exit codes: `0` verified, `1` tool error (connection, binaries…), `2`
verification mismatch when `--fail-on-mismatch` is set.

### Cron example (daily at 02:30, alert on failure)

```cron
30 2 * * * PGPASSWORD=$(cat /etc/pgbv/pass) pgbv verify --db shop --out /backups \
           --retention 14 --report /var/log/pgbv/last.json --fail-on-mismatch \
           || curl -fsS https://hc-ping.com/<uuid>/fail
```

### Report format

```json
{
  "source_db": "shop",
  "dump_file": "/backups/shop-20261007-090001.dump",
  "dump_seconds": 1.9, "restore_seconds": 2.1, "dump_bytes": 188621,
  "tables_source": 12, "tables_restored": 12,
  "rows_source": 48312, "rows_restored": 48312,
  "mismatches": [], "ok": true, "error": null
}
```

## Tests

```bash
pip install -e .[dev]
pytest                                   # unit tests
PGBV_TEST_DSN="host=localhost user=postgres" pytest   # + full round-trip
```

## Design choices

* Custom-format dumps (`-Fc`): compressed, selective restore possible.
* The scratch database is created and dropped by the tool; the source is
  only ever read.
* Exact `count(*)` rather than `pg_class.reltuples` estimates — slower on
  very large tables but it is the whole point of the tool to be sure.
* Pure standard library + `psycopg2`; no framework, easy to audit.

MIT License — Florian Whannou
