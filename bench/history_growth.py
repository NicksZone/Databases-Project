"""Query time as the history grows, with and without PARTITION BY SYSTEM_TIME.

Usage:
    python -m bench.history_growth                 # levels 1, 10, 100, 500, 1000 versions per article
    python -m bench.history_growth --levels 1 100

The real data has at most ~100 stored versions per article, so to see what
happens beyond that, this builds a table `growth` with the same definition as
page_content and fills it with N versions of each of 5 articles, for growing N.
The texts are real (each article's own revisions, cycled in order), the dates
are evenly spaced from 2001-01-01 to 2026-10-01. Loaded with
system_versioning_insert_history, like the real loader.

For each level, twice (plain, then PARTITION BY SYSTEM_TIME with one history and
one current partition), it measures:

    as_of_new_ms    AS OF a moment after the last version (the current one)
    as_of_mid_ms    AS OF the middle of the history
    as_of_old_ms    AS OF a moment just after the first version
    current_ms      the same lookup without FOR SYSTEM_TIME (current rows only)
    count_current_ms  SELECT COUNT(*) over the whole table (current rows only)
    rows examined   ANALYZE's r_rows for AS OF old and new: why the times differ

Each time is the median over the 5 articles x 30 repeats, warm buffer pool.
The table is dropped at the end. Results: bench/results/history_growth.json.
"""

import argparse
import json
import re
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path

from timemachine.db import connect
from timemachine.fetch import ROOT
from timemachine.load import BATCH_BYTES, read_article

RESULTS = ROOT / "bench" / "results" / "history_growth.json"
ARTICLES = ["Pluto", "MariaDB", "SQL", "Tbilisi", "Chess"]
FIRST, LAST = datetime(2001, 1, 1), datetime(2026, 10, 1)

DDL = """
CREATE OR REPLACE TABLE growth (
    page_id INT UNSIGNED NOT NULL PRIMARY KEY,
    rev_id INT UNSIGNED NOT NULL,
    content MEDIUMTEXT NOT NULL,
    sys_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
    sys_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
    PERIOD FOR SYSTEM_TIME (sys_start, sys_end)
) ENGINE=InnoDB WITH SYSTEM VERSIONING
"""
PARTITIONED = " PARTITION BY SYSTEM_TIME (PARTITION p_history HISTORY, PARTITION p_current CURRENT)"


def fill(conn, articles, n: int) -> None:
    cur = conn.cursor()
    step = (LAST - FIRST) / n
    for a in articles:
        revs = a["revisions"]
        rows = [(a["page_id"], revs[i % len(revs)]["rev_id"], revs[i % len(revs)]["content"],
                 FIRST + step * i, FIRST + step * (i + 1)) for i in range(n)]
        history, current = rows[:-1], rows[-1]
        batch, size = [], 0
        for row in history:
            if batch and size + len(row[2]) > BATCH_BYTES:
                cur.executemany("INSERT INTO growth (page_id, rev_id, content, sys_start, sys_end) VALUES (?, ?, ?, ?, ?)", batch)
                batch, size = [], 0
            batch.append(row)
            size += len(row[2])
        if batch:
            cur.executemany("INSERT INTO growth (page_id, rev_id, content, sys_start, sys_end) VALUES (?, ?, ?, ?, ?)", batch)
        cur.execute("INSERT INTO growth (page_id, rev_id, content, sys_start) VALUES (?, ?, ?, ?)", current[:4])
        conn.commit()


def median_ms(cur, sql: str, args_list: list[tuple], repeat: int = 30) -> float:
    for args in args_list:
        cur.execute(sql, args)
        cur.fetchall()
    times = []
    for _ in range(repeat):
        for args in args_list:
            start = time.perf_counter()
            cur.execute(sql, args)
            cur.fetchall()
            times.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(times), 3)


def rows_examined(cur, sql: str, args: tuple) -> float:
    """r_rows of the growth table from ANALYZE FORMAT=JSON: rows the server actually read."""
    cur.execute("ANALYZE FORMAT=JSON " + sql, args)
    plan = cur.fetchone()[0]
    found = re.findall(r'"table_name":\s*"growth".*?"r_rows":\s*([\d.]+)', plan, re.S)
    return float(found[0]) if found else -1


def measure(conn, articles, n: int, partitioned: bool) -> dict:
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(DDL + (PARTITIONED if partitioned else ""))
    conn.autocommit = False
    cur.execute("SET system_versioning_insert_history = ON")
    start = time.perf_counter()
    fill(conn, articles, n)
    load_s = time.perf_counter() - start
    cur.execute("SET system_versioning_insert_history = OFF")
    conn.autocommit = True

    step = (LAST - FIRST) / n
    moments = {
        "new": LAST + timedelta(days=1),
        "mid": FIRST + step * (n // 2) + timedelta(seconds=1),
        "old": FIRST + timedelta(seconds=1),
    }
    ids = [a["page_id"] for a in articles]
    as_of = "SELECT rev_id, LENGTH(content) FROM growth FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?"
    result = {"versions_per_article": n, "partitioned": partitioned, "rows": n * len(articles), "load_s": round(load_s, 2)}
    for name, moment in moments.items():
        result[f"as_of_{name}_ms"] = median_ms(cur, as_of, [(moment, i) for i in ids])
    result["current_ms"] = median_ms(cur, "SELECT rev_id, LENGTH(content) FROM growth WHERE page_id = ?", [(i,) for i in ids])
    result["count_current_ms"] = median_ms(cur, "SELECT COUNT(*) FROM growth", [()], repeat=10)
    result["rows_examined_as_of_old"] = rows_examined(cur, as_of, (moments["old"], ids[0]))
    result["rows_examined_as_of_new"] = rows_examined(cur, as_of, (moments["new"], ids[0]))
    result["rows_examined_count_current"] = rows_examined(cur, "SELECT COUNT(*) FROM growth", ())

    # Size from the .ibd files themselves (one per partition). information_schema's
    # data_length, read right after loading, left out the overflow pages that hold
    # the long texts, and reported 0.4 MB for 3 MB of text.
    cur.execute("SELECT @@datadir, DATABASE()")
    datadir, db = cur.fetchone()
    files = {p.stem: p.stat().st_size for p in (Path(datadir) / db).glob("growth*.ibd")}
    result["size_mb"] = round(sum(files.values()) / 2**20, 1)
    if partitioned:
        result["partition_mb"] = {name.split("#")[-1]: round(size / 2**20, 1) for name, size in files.items()}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 10, 100, 500, 1000])
    args = parser.parse_args()

    from timemachine.fetch import RAW_DIR
    articles = [read_article(p) for p in sorted(RAW_DIR.glob("*.json"))]
    articles = [a for a in articles if a["title"] in ARTICLES]
    if len(articles) != len(ARTICLES):
        raise SystemExit("Missing articles in data/raw/. Run: python -m timemachine.fetch")

    conn = connect()
    results = []
    for n in args.levels:
        for partitioned in (False, True):
            r = measure(conn, articles, n, partitioned)
            results.append(r)
            print(r, flush=True)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS growth")
    cur.execute("SELECT @@version")
    version = cur.fetchone()[0]

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps({"mariadb": version, "articles": ARTICLES, "results": results}, indent=2), encoding="utf-8")
    print(f"\nsaved {RESULTS.relative_to(ROOT)}\n")
    print("| versions / article | partitioned | size | AS OF new | AS OF mid | AS OF old | rows read (old / new) | current lookup | COUNT(*) current |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['versions_per_article']} | {'yes' if r['partitioned'] else 'no'} | {r['size_mb']} MB "
              f"| {r['as_of_new_ms']} ms | {r['as_of_mid_ms']} ms | {r['as_of_old_ms']} ms "
              f"| {r['rows_examined_as_of_old']:g} / {r['rows_examined_as_of_new']:g} "
              f"| {r['current_ms']} ms | {r['count_current_ms']} ms |")


if __name__ == "__main__":
    main()
