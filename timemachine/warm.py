"""Render every stored version ahead of time, so no page view has to wait.

Usage:
    python -m timemachine.warm             # render what is not cached yet
    python -m timemachine.warm --rebuild   # empty the cache first (after changing render.py)

The web page fills the `rendered` cache by itself on first view, so this is
optional. It is worth running before a demo: rendering runs on every CPU core
here, instead of one long article at a time while someone is watching.
"""

import argparse
import json
import os
import time
from multiprocessing import Pool

from timemachine import queries
from timemachine.db import connect
from timemachine.render import to_paragraphs


def render_one(item: tuple[int, str]) -> tuple[int, str]:
    rev_id, wikitext = item
    return rev_id, json.dumps(to_paragraphs(wikitext), ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild", action="store_true", help="empty the cache and render everything again")
    args = parser.parse_args()

    conn = connect(autocommit=True)
    cur = conn.cursor()
    if args.rebuild:
        cur.execute("TRUNCATE TABLE rendered")
    cur.execute(queries.UNRENDERED_VERSIONS)
    todo = cur.fetchall()
    if not todo:
        print("every stored version is already rendered")
        return

    workers = os.cpu_count() or 1
    print(f"rendering {len(todo)} versions on {workers} cores ...")
    start = time.perf_counter()
    with Pool(workers) as pool:
        for done, (rev_id, paragraphs) in enumerate(pool.imap_unordered(render_one, todo, chunksize=4), 1):
            cur.execute(queries.SAVE_RENDERED, (rev_id, paragraphs))
            if done % 100 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)}")
    print(f"done in {time.perf_counter() - start:.0f} s")
    conn.close()


if __name__ == "__main__":
    main()
