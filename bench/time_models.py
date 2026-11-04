"""Three ways to give a system-versioned history its real dates, compared.

Usage:
    python -m bench.time_models            # 3 loads per method (about 1 minute)
    python -m bench.time_models --keep     # leave the tm_* tables behind to explore

Loads the same revisions (data/raw) three ways, each into its own table:

    replay          SET timestamp = <edit time>, then INSERT/UPDATE, one statement
                    per revision. The server computes every period. (the loader today)
    insert_history  SET system_versioning_insert_history = ON, then INSERT the
                    history rows with sys_start and sys_end written out, one batch
                    and one transaction per article.
    app_period      an application-time period (valid_from, valid_to) for the
                    Wikipedia dates, with UNIQUE ... WITHOUT OVERLAPS, on a table
                    that is also system-versioned: system time = when we loaded it.

and measures, for each:
    - load time (median of the runs)
    - size (data + indexes, after ANALYZE TABLE)
    - the time to answer "article X as of moment T" for 200 sampled (X, T) pairs
    - that all three return the same revision for every one of those pairs
    - what each does with a bad input (the integrity probe)

Results: bench/results/time_models.json, and a Markdown table on stdout.
"""

import argparse
import json
import random
import statistics
import time
from datetime import datetime, timezone

from timemachine.db import connect
from timemachine.fetch import RAW_DIR, ROOT

RESULTS = ROOT / "bench" / "results" / "time_models.json"
FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, 999999)  # open end of an application-time period

COLUMNS = """
    page_id      INT UNSIGNED NOT NULL,
    rev_id       INT UNSIGNED NOT NULL,
    editor       VARCHAR(255) NULL,
    edit_comment TEXT NULL,
    content      MEDIUMTEXT NOT NULL,
"""
SYSTEM_PERIOD = """
    sys_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
    sys_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
    PERIOD FOR SYSTEM_TIME (sys_start, sys_end)
"""
DDL = {
    # the same definition as page_content in sql/schema.sql
    "replay": f"CREATE OR REPLACE TABLE tm_replay ({COLUMNS} {SYSTEM_PERIOD}, PRIMARY KEY (page_id)) WITH SYSTEM VERSIONING",
    "insert_history": f"CREATE OR REPLACE TABLE tm_insert_history ({COLUMNS} {SYSTEM_PERIOD}, PRIMARY KEY (page_id)) WITH SYSTEM VERSIONING",
    # one row per revision in application time, so page_id alone is not unique
    "app_period": f"""CREATE OR REPLACE TABLE tm_app_period ({COLUMNS}
        valid_from DATETIME(6) NOT NULL,
        valid_to   DATETIME(6) NOT NULL,
        PERIOD FOR wiki_time (valid_from, valid_to),
        {SYSTEM_PERIOD},
        PRIMARY KEY (page_id, valid_to),
        UNIQUE KEY no_overlap (page_id, wiki_time WITHOUT OVERLAPS)
    ) WITH SYSTEM VERSIONING""",
}
AS_OF = {
    "replay": "SELECT rev_id, LENGTH(content) FROM tm_replay FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?",
    "insert_history": "SELECT rev_id, LENGTH(content) FROM tm_insert_history FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?",
    "app_period": ("SELECT rev_id, LENGTH(content) FROM tm_app_period "
                   "WHERE valid_from <= ? AND valid_to > ? AND page_id = ?"),
}


def read_articles() -> list[dict]:
    """Every article with its revisions oldest first, timestamps as UTC datetimes,
    and revisions that would give a zero-length period dropped (as the loader does)."""
    articles = []
    for path in sorted(RAW_DIR.glob("*.json")):
        article = json.loads(path.read_text(encoding="utf-8"))
        revs, last = [], None
        for rev in article["revisions"]:
            ts = datetime.fromisoformat(rev["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
            if last is None or ts > last:
                revs.append({**rev, "ts": ts})
                last = ts
        articles.append({"page_id": article["page_id"], "title": article["title"], "revisions": revs})
    return articles


# ---------- the three loaders ----------

BATCH_BYTES = 4 * 2**20


def insert_in_batches(cur, sql: str, rows: list[tuple]) -> None:
    """executemany, in chunks of at most ~4 MB of text. One article's whole history
    in one batch can pass max_allowed_packet (16 MB by default: Barack Obama's is
    more), and the server then drops the connection."""
    batch, size = [], 0
    for row in rows:
        row_size = len(row[4].encode("utf-8"))  # content is the 5th column
        if batch and size + row_size > BATCH_BYTES:
            cur.executemany(sql, batch)
            batch, size = [], 0
        batch.append(row)
        size += row_size
    if batch:
        cur.executemany(sql, batch)

def load_replay(conn, articles):
    conn.autocommit = True  # each statement is its own transaction and takes the timestamp just set
    cur = conn.cursor()
    for a in articles:
        for i, rev in enumerate(a["revisions"]):
            cur.execute("SET timestamp = ?", (rev["ts"].replace(tzinfo=timezone.utc).timestamp(),))
            values = (rev["rev_id"], rev["user"], rev["comment"], rev["content"], a["page_id"])
            if i == 0:
                cur.execute("INSERT INTO tm_replay (rev_id, editor, edit_comment, content, page_id) VALUES (?, ?, ?, ?, ?)", values)
            else:
                cur.execute("UPDATE tm_replay SET rev_id = ?, editor = ?, edit_comment = ?, content = ? WHERE page_id = ?", values)
    cur.execute("SET timestamp = DEFAULT")


def load_insert_history(conn, articles):
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET system_versioning_insert_history = ON")
    for a in articles:
        revs = a["revisions"]
        # every version but the last ends where the next one starts
        insert_in_batches(
            cur,
            "INSERT INTO tm_insert_history (page_id, rev_id, editor, edit_comment, content, sys_start, sys_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(a["page_id"], r["rev_id"], r["user"], r["comment"], r["content"], r["ts"], revs[i + 1]["ts"])
             for i, r in enumerate(revs[:-1])],
        )
        # the current version: sys_end left out, so the server makes it current
        last = revs[-1]
        cur.execute(
            "INSERT INTO tm_insert_history (page_id, rev_id, editor, edit_comment, content, sys_start) VALUES (?, ?, ?, ?, ?, ?)",
            (a["page_id"], last["rev_id"], last["user"], last["comment"], last["content"], last["ts"]),
        )
        conn.commit()  # one transaction per article: its history loads whole or not at all
    cur.execute("SET system_versioning_insert_history = OFF")


def load_app_period(conn, articles):
    conn.autocommit = False
    cur = conn.cursor()
    for a in articles:
        revs = a["revisions"]
        insert_in_batches(
            cur,
            "INSERT INTO tm_app_period (page_id, rev_id, editor, edit_comment, content, valid_from, valid_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(a["page_id"], r["rev_id"], r["user"], r["comment"], r["content"], r["ts"],
              revs[i + 1]["ts"] if i + 1 < len(revs) else FAR_FUTURE)
             for i, r in enumerate(revs)],
        )
        conn.commit()


LOADERS = {"replay": load_replay, "insert_history": load_insert_history, "app_period": load_app_period}


# ---------- measuring ----------

def sample_moments(articles, n_per_article: int = 10, seed: int = 2026) -> list[tuple[int, datetime]]:
    """(page_id, moment) pairs, from a year before each article's first version to now,
    so some fall before the article existed."""
    rng = random.Random(seed)
    now = datetime(2026, 10, 25)
    pairs = []
    for a in articles:
        lo = a["revisions"][0]["ts"].timestamp() - 365 * 86400
        hi = now.timestamp()
        for _ in range(n_per_article):
            pairs.append((a["page_id"], datetime.fromtimestamp(rng.uniform(lo, hi)).replace(microsecond=0)))
    return pairs


def answer(cur, method: str, page_id: int, moment: datetime):
    args = (moment, moment, page_id) if method == "app_period" else (moment, page_id)
    cur.execute(AS_OF[method], args)
    rows = cur.fetchall()
    return [r[0] for r in rows]


def time_queries(cur, method, pairs, passes: int = 5) -> dict:
    for page_id, moment in pairs:  # warm up
        answer(cur, method, page_id, moment)
    per_query = []
    for _ in range(passes):
        for page_id, moment in pairs:
            start = time.perf_counter()
            answer(cur, method, page_id, moment)
            per_query.append((time.perf_counter() - start) * 1000)
    per_query.sort()
    return {"query_ms_median": round(statistics.median(per_query), 3),
            "query_ms_p95": round(per_query[int(len(per_query) * 0.95)], 3)}


def size_mb(cur, table: str) -> float:
    cur.execute(f"ANALYZE TABLE {table}")
    cur.fetchall()
    cur.execute("SELECT data_length + index_length FROM information_schema.TABLES "
                "WHERE table_schema = DATABASE() AND table_name = ?", (table,))
    return round(int(cur.fetchone()[0]) / 2**20, 1)


# ---------- what each method does with a bad input ----------

def probe(conn) -> dict:
    """Try to break each method's history on a throwaway page (id 0), then clean up."""
    conn.autocommit = True
    cur = conn.cursor()
    out = {}

    # replay: an UPDATE with the clock set before the current version started
    cur.execute("SET timestamp = UNIX_TIMESTAMP('2010-01-01 00:00:00')")
    cur.execute("INSERT INTO tm_replay (page_id, rev_id, content) VALUES (0, 1, 'v1')")
    cur.execute("SET timestamp = UNIX_TIMESTAMP('2005-01-01 00:00:00')")
    try:
        cur.execute("UPDATE tm_replay SET rev_id = 2, content = 'v2' WHERE page_id = 0")
        cur.execute("SET timestamp = DEFAULT")
        cur.execute("SELECT rev_id, sys_start, sys_end FROM tm_replay FOR SYSTEM_TIME ALL WHERE page_id = 0 ORDER BY rev_id")
        out["replay"] = "accepted, rows: " + "; ".join(f"rev {r} {s} .. {e}" for r, s, e in cur.fetchall())
    except Exception as err:
        out["replay"] = f"rejected: {err}"
    cur.execute("SET timestamp = DEFAULT")

    # insert_history: two history rows that overlap in time
    cur.execute("SET system_versioning_insert_history = ON")
    cur.execute("INSERT INTO tm_insert_history (page_id, rev_id, content, sys_start, sys_end) "
                "VALUES (0, 1, 'v1', '2001-01-01', '2003-01-01'), (0, 2, 'v2', '2002-01-01', '2004-01-01')")
    cur.execute("SET system_versioning_insert_history = OFF")
    cur.execute("SELECT COUNT(*) FROM tm_insert_history FOR SYSTEM_TIME AS OF TIMESTAMP '2002-06-01 00:00:00' WHERE page_id = 0")
    out["insert_history"] = f"accepted, and AS OF 2002-06-01 now returns {cur.fetchone()[0]} versions of one page"

    # app_period: the same overlap
    cur.execute("INSERT INTO tm_app_period (page_id, rev_id, content, valid_from, valid_to) VALUES (0, 1, 'v1', '2001-01-01', '2003-01-01')")
    try:
        cur.execute("INSERT INTO tm_app_period (page_id, rev_id, content, valid_from, valid_to) VALUES (0, 2, 'v2', '2002-01-01', '2004-01-01')")
        out["app_period"] = "accepted"
    except Exception as err:
        out["app_period"] = f"rejected: {err}"

    # clean up the throwaway page, history included
    for table in ("tm_replay", "tm_insert_history", "tm_app_period"):
        cur.execute(f"DELETE FROM {table} WHERE page_id = 0")
        cur.execute(f"DELETE HISTORY FROM {table} BEFORE SYSTEM_TIME NOW(6) + INTERVAL 1 DAY")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3, help="full loads per method")
    parser.add_argument("--keep", action="store_true", help="keep the tm_* tables afterwards")
    args = parser.parse_args()

    articles = read_articles()
    if not articles:
        raise SystemExit("No data in data/raw/. Run: python -m timemachine.fetch")
    pairs = sample_moments(articles)
    conn = connect(autocommit=True)
    cur = conn.cursor()

    results, answers = {}, {}
    for method in DDL:
        load_times = []
        for _ in range(args.runs):
            conn.autocommit = True
            cur.execute(DDL[method])
            start = time.perf_counter()
            LOADERS[method](conn, articles)
            load_times.append(time.perf_counter() - start)
        conn.autocommit = True
        table = f"tm_{method}"
        cur.execute(f"SELECT COUNT(*) FROM {table} FOR SYSTEM_TIME ALL")
        rows = cur.fetchone()[0]
        answers[method] = [answer(cur, method, p, m) for p, m in pairs]
        results[method] = {
            "load_s_median": round(statistics.median(load_times), 2),
            "load_s_runs": [round(t, 2) for t in load_times],
            "rows": rows,
            "size_mb": size_mb(cur, table),
            **time_queries(cur, method, pairs),
        }
        print(method, results[method], flush=True)

    # do they agree?
    disagreements = [
        (p, m.isoformat(), {k: answers[k][i] for k in answers})
        for i, (p, m) in enumerate(pairs)
        if len({tuple(answers[k][i]) for k in answers}) > 1
    ]
    multi = sum(1 for k in answers for a in answers[k] if len(a) > 1)
    missing = sum(1 for a in answers["replay"] if not a)
    print(f"\n{len(pairs)} sampled questions, {len(disagreements)} disagreements, "
          f"{missing} before the article existed, {multi} answers with more than one row")

    integrity = probe(conn)
    for k, v in integrity.items():
        print(f"probe {k}: {v}")

    if not args.keep:
        conn.autocommit = True
        cur.execute("DROP TABLE IF EXISTS tm_replay, tm_insert_history, tm_app_period")

    cur.execute("SELECT @@version, @@innodb_flush_log_at_trx_commit, @@secure_timestamp")
    version, flush_at_commit, secure_ts = cur.fetchone()
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps({
        "mariadb": version,
        "innodb_flush_log_at_trx_commit": int(flush_at_commit),
        "secure_timestamp": secure_ts,
        "articles": len(articles),
        "revisions": sum(len(a["revisions"]) for a in articles),
        "sampled_questions": len(pairs),
        "disagreements": disagreements,
        "results": results,
        "integrity_probe": integrity,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {RESULTS.relative_to(ROOT)}\n")
    print("| method | load | rows | size | AS OF query, median | p95 |")
    print("|---|---|---|---|---|---|")
    for k, r in results.items():
        print(f"| `{k}` | {r['load_s_median']} s | {r['rows']} | {r['size_mb']} MB | {r['query_ms_median']} ms | {r['query_ms_p95']} ms |")


if __name__ == "__main__":
    main()
