"""(Re)create the schema and load the downloaded revisions into MariaDB.

Usage:
    python -m timemachine.load                     # system_versioning_insert_history (default)
    python -m timemachine.load --method replay     # SET timestamp + INSERT/UPDATE per revision

Run python -m timemachine.fetch first. Every run starts from an empty database,
so loading twice gives the same result as loading once.

How the history gets its real dates
-----------------------------------
A system-versioned table stamps each row version with the time the statement
ran. Loading revisions the naive way would give a perfect history of this
script, all of it from the last few seconds, and nothing about the article.
Both methods here give the server the real Wikipedia dates instead:

insert-history (default): with system_versioning_insert_history = ON, the
    history rows are inserted with sys_start and sys_end written out, every
    version ending where the next one starts. The current version is inserted
    without sys_end, so the server makes it current. One transaction per
    article: its history loads whole or not at all.

replay: before each statement, set the session clock to the revision's time:
        SET timestamp = <revision time>;
        UPDATE page_content SET content = ... WHERE page_id = ...;
    The server ends the previous version and starts the new one at that moment.
    Every statement must be its own transaction to take its own timestamp.

Both need secure_timestamp = NO (the default). bench/time_models.py compares
them with an application-time period, and the README explains the choice.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from timemachine.db import connect
from timemachine.fetch import RAW_DIR, ROOT

SCHEMA_FILE = ROOT / "sql" / "schema.sql"
BATCH_BYTES = 4 * 2**20  # stay well under max_allowed_packet (16 MB by default)

INSERT_HISTORY_SQL = ("INSERT INTO page_content (page_id, rev_id, editor, edit_comment, content, sys_start, sys_end) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)")
INSERT_CURRENT_SQL = ("INSERT INTO page_content (page_id, rev_id, editor, edit_comment, content, sys_start) "
                      "VALUES (?, ?, ?, ?, ?, ?)")


def run_schema(cur, schema_sql: str | None = None) -> None:
    """Run sql/schema.sql, or a variant of it (bench/compression.py passes one)."""
    schema_sql = schema_sql or SCHEMA_FILE.read_text(encoding="utf-8")
    lines = [l for l in schema_sql.splitlines() if not l.strip().startswith("--")]
    for statement in "\n".join(lines).split(";"):
        if statement.strip():
            cur.execute(statement)


def read_article(path: Path) -> dict:
    """One downloaded article, its revisions oldest first with UTC datetimes.
    A revision whose time is not later than the one before is dropped: a version
    needs a period longer than zero, and with replay an earlier clock would make
    the server silently discard the version before it (see bench/time_models.py)."""
    article = json.loads(path.read_text(encoding="utf-8"))
    revisions, last = [], None
    for rev in article["revisions"]:
        # Wikipedia gives e.g. 2006-08-24T14:30:05Z
        ts = datetime.fromisoformat(rev["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        if last is None or ts > last:
            revisions.append({**rev, "ts": ts})
            last = ts
    return {"page_id": article["page_id"], "title": article["title"], "revisions": revisions}


def load_insert_history(conn, article: dict) -> int:
    cur = conn.cursor()
    revs, page_id = article["revisions"], article["page_id"]
    cur.execute("INSERT INTO pages (page_id, title) VALUES (?, ?)", (page_id, article["title"]))

    # Every version but the last ends exactly where the next one starts, so the
    # periods chain without gaps or overlaps by construction. The server does
    # not check this for us: with insert_history it accepts overlapping rows.
    history = [(page_id, r["rev_id"], r["user"], r["comment"], r["content"], r["ts"], revs[i + 1]["ts"])
               for i, r in enumerate(revs[:-1])]
    batch, size = [], 0
    for row in history:
        if batch and size + len(row[4].encode("utf-8")) > BATCH_BYTES:
            cur.executemany(INSERT_HISTORY_SQL, batch)
            batch, size = [], 0
        batch.append(row)
        size += len(row[4].encode("utf-8"))
    if batch:
        cur.executemany(INSERT_HISTORY_SQL, batch)

    last = revs[-1]
    cur.execute(INSERT_CURRENT_SQL, (page_id, last["rev_id"], last["user"], last["comment"], last["content"], last["ts"]))
    conn.commit()  # the article's whole history, or none of it
    return len(revs)


def load_replay(conn, article: dict) -> int:
    cur = conn.cursor()
    page_id = article["page_id"]
    cur.execute("INSERT INTO pages (page_id, title) VALUES (?, ?)", (page_id, article["title"]))
    for i, rev in enumerate(article["revisions"]):
        cur.execute("SET timestamp = ?", (rev["ts"].replace(tzinfo=timezone.utc).timestamp(),))
        values = (rev["rev_id"], rev["user"], rev["comment"], rev["content"], page_id)
        if i == 0:
            cur.execute("INSERT INTO page_content (rev_id, editor, edit_comment, content, page_id) VALUES (?, ?, ?, ?, ?)", values)
        else:
            cur.execute("UPDATE page_content SET rev_id = ?, editor = ?, edit_comment = ?, content = ? WHERE page_id = ?", values)
    cur.execute("SET timestamp = DEFAULT")
    return len(article["revisions"])


def load_article(cur, path: Path) -> int:
    """Replay one article on an autocommit cursor (used by bench/compression.py)."""
    return load_replay(cur.connection, read_article(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--method", choices=["insert-history", "replay"], default="insert-history")
    args = parser.parse_args()

    files = sorted(RAW_DIR.glob("*.json"))
    if not files:
        raise SystemExit("No data in data/raw/. Run: python -m timemachine.fetch")

    conn = connect(autocommit=True)
    cur = conn.cursor()
    run_schema(cur)
    print(f"schema created, loading with {args.method}")

    if args.method == "insert-history":
        conn.autocommit = False
        cur.execute("SET system_versioning_insert_history = ON")
        load = load_insert_history
    else:
        load = load_replay  # autocommit stays on: each statement takes the timestamp just set

    total = 0
    for path in files:
        n = load(conn, read_article(path))
        total += n
        print(f"loaded {path.stem}: {n} versions")

    conn.autocommit = True
    cur.execute("SET system_versioning_insert_history = OFF")
    cur.execute("""
        SELECT MIN(sys_start), MAX(sys_start), COUNT(*)
        FROM page_content FOR SYSTEM_TIME ALL
    """)
    oldest, newest, rows = cur.fetchone()
    print(f"\n{total} versions of {len(files)} articles, {rows} rows in page_content")
    print(f"history spans {oldest} .. {newest} (UTC)")
    conn.close()


if __name__ == "__main__":
    main()
