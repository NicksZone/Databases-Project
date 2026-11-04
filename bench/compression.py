"""How much does compression save on a system-versioned table, and what does it cost?

Usage:
    python -m bench.compression            # 3 runs per variant (about 3 minutes)
    python -m bench.compression --runs 5

Loads the same data (data/raw) into page_content four ways and measures each:

    none     the schema as it was: MEDIUMTEXT, ROW_FORMAT=DYNAMIC
    column   content MEDIUMTEXT COMPRESSED    MariaDB column compression (zlib, per value)
    page     PAGE_COMPRESSED=1                InnoDB page compression (zlib, per 16 KB page, sparse file)
    row      ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8   the older InnoDB compressed row format

For each: load time, size on disk, and the time of three queries that have to
read (and decompress) the text. Results go to bench/results/compression.json
and are printed as a Markdown table for the README.

Caveats, also in the README: one machine, the whole table fits in the buffer
pool (2 GB), so query times are warm-cache times, not disk reads.

Needs one extra privilege, to force InnoDB to write pages to disk before the
sizes are read (see flush_to_disk). As root, once:
    GRANT RELOAD ON *.* TO 'wiki'@'localhost';
"""

import argparse
import ctypes
import json
import os
import platform
import re
import statistics
import time
from pathlib import Path

from timemachine.db import connect
from timemachine.fetch import RAW_DIR, ROOT
from timemachine.load import SCHEMA_FILE, load_article, run_schema

RESULTS = ROOT / "bench" / "results" / "compression.json"
TABLE_OPTIONS = re.compile(r"\) ENGINE=InnoDB(\s+WITH SYSTEM VERSIONING)")
CONTENT_COLUMN = re.compile(r"(content\s+MEDIUMTEXT)")

VARIANTS = {
    "none": lambda sql: sql,
    "column": lambda sql: CONTENT_COLUMN.sub(r"\1 COMPRESSED", sql, count=1),
    "page": lambda sql: TABLE_OPTIONS.sub(r") ENGINE=InnoDB PAGE_COMPRESSED=1\1", sql, count=1),
    "row": lambda sql: TABLE_OPTIONS.sub(r") ENGINE=InnoDB ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8\1", sql, count=1),
}

QUERIES = {
    # one article as of a date: decompresses one version
    "as_of_ms": ("SELECT LENGTH(content) FROM page_content FOR SYSTEM_TIME AS OF TIMESTAMP '2026-01-01 00:00:00' "
                 "WHERE page_id = (SELECT page_id FROM pages WHERE title = 'Barack Obama')"),
    # the timeline query: LENGTH() of every version of one article
    "timeline_ms": ("SELECT c.rev_id, LENGTH(c.content) FROM page_content FOR SYSTEM_TIME ALL AS c "
                    "JOIN pages p USING (page_id) WHERE p.title = 'Barack Obama'"),
    # touch every version of every article
    "full_scan_ms": "SELECT SUM(LENGTH(content)) FROM page_content FOR SYSTEM_TIME ALL",
}


def allocated_bytes(path: Path) -> int:
    """Bytes actually taken on disk. Page compression punches holes in the file,
    so this can be much smaller than the file size."""
    if platform.system() == "Windows":
        get_size = ctypes.windll.kernel32.GetCompressedFileSizeW
        get_size.restype = ctypes.c_uint32
        high = ctypes.c_uint32(0)
        low = get_size(str(path), ctypes.byref(high))
        return (high.value << 32) + low
    return os.stat(path).st_blocks * 512


def flush_to_disk(cur) -> float:
    """Make InnoDB write every page of page_content to the .ibd file, so the sizes
    are final. Page compression only punches its holes when a page is written,
    and with the default innodb_max_dirty_pages_pct_lwm=0 an idle server leaves
    dirty pages in memory indefinitely, so waiting is not enough.
    FLUSH TABLES ... FOR EXPORT forces the write, and needs the RELOAD privilege."""
    start = time.perf_counter()
    try:
        cur.execute("FLUSH TABLES page_content FOR EXPORT")
    except Exception as err:
        raise SystemExit(
            f"{err}\nThe benchmark needs to force InnoDB to write to disk. As root, run once:\n"
            "    GRANT RELOAD ON *.* TO 'wiki'@'localhost';"
        )
    cur.execute("UNLOCK TABLES")
    return time.perf_counter() - start


def time_query(cur, sql: str, repeat: int = 15) -> float:
    cur.execute(sql)  # once to warm up
    cur.fetchall()
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        cur.execute(sql)
        cur.fetchall()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def run_variant(name: str, runs: int, files: list[Path], datadir: Path) -> dict:
    schema = VARIANTS[name](SCHEMA_FILE.read_text(encoding="utf-8"))
    if name != "none" and schema == SCHEMA_FILE.read_text(encoding="utf-8"):
        raise SystemExit(f"variant {name!r} did not change the schema, check the patterns")

    conn = connect(autocommit=True)
    cur = conn.cursor()
    load_times = []
    for _ in range(runs):
        start = time.perf_counter()
        run_schema(cur, schema)
        for path in files:
            load_article(cur, path)
        load_times.append(time.perf_counter() - start)

    flush_wait = flush_to_disk(cur)
    cur.execute("ANALYZE TABLE page_content")
    cur.fetchall()
    cur.execute("SELECT data_length FROM information_schema.TABLES "
                "WHERE table_schema = DATABASE() AND table_name = 'page_content'")
    data_length = int(cur.fetchone()[0])
    ibd = datadir / os.getenv("DB_NAME", "wiki_timemachine") / "page_content.ibd"

    result = {
        "variant": name,
        "load_s_median": round(statistics.median(load_times), 2),
        "load_s_runs": [round(t, 2) for t in load_times],
        "file_mb": round(ibd.stat().st_size / 2**20, 1),
        "on_disk_mb": round(allocated_bytes(ibd) / 2**20, 1),
        "innodb_data_length_mb": round(data_length / 2**20, 1),
        "flush_s": round(flush_wait, 1),
    }
    for key, sql in QUERIES.items():
        result[key] = round(time_query(cur, sql), 2)
    conn.close()
    return result


def markdown(results: list[dict]) -> str:
    base = next(r for r in results if r["variant"] == "none")
    lines = [
        "| variant | on disk | vs none | load | AS OF one version | timeline (92 versions) | full scan (1,708 versions) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r['variant']}` | {r['on_disk_mb']} MB | {r['on_disk_mb'] / base['on_disk_mb']:.0%} "
            f"| {r['load_s_median']} s | {r['as_of_ms']} ms | {r['timeline_ms']} ms | {r['full_scan_ms']} ms |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3, help="full loads per variant")
    parser.add_argument("--variants", nargs="+", choices=list(VARIANTS), default=list(VARIANTS))
    args = parser.parse_args()

    files = sorted(RAW_DIR.glob("*.json"))
    if not files:
        raise SystemExit("No data in data/raw/. Run: python -m timemachine.fetch")
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT @@datadir, @@version, @@innodb_buffer_pool_size, @@innodb_compression_algorithm, "
                "@@column_compression_zlib_level")
    datadir, version, pool, algorithm, zlib_level = cur.fetchone()
    conn.close()

    results = []
    for name in args.variants:
        print(f"{name}: {args.runs} loads ...", flush=True)
        results.append(run_variant(name, args.runs, files, Path(datadir)))
        print("   ", results[-1], flush=True)

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps({
        "mariadb": version,
        "machine": f"{platform.system()} {platform.release()}, {os.cpu_count()} logical cores",
        "innodb_buffer_pool_mb": int(pool) // 2**20,
        "innodb_compression_algorithm": algorithm,
        "column_compression_zlib_level": int(zlib_level),
        "runs_per_variant": args.runs,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nsaved {RESULTS.relative_to(ROOT)}\n")
    print(markdown(results))
    print("\npage_content now holds the last variant. Run python -m timemachine.load to restore sql/schema.sql.")


if __name__ == "__main__":
    main()
