A project for area 3, "System-versioned tables: history for free" for the [MariaDB student database projects, 2026-09](https://mariadb.org/bachelor_hackathon_2026-09/).

# Wikipedia Time Machine

Pick a Wikipedia article and a moment in time, and see the article as it stood then. The history is kept by MariaDB itself: one `WITH SYSTEM VERSIONING` clause on a table, read back with `FOR SYSTEM_TIME AS OF`. There are no revision tables, no triggers and no history code in the application.

There's an irony here. Wikipedia runs on MariaDB, but it keeps its history in application-level revision tables that were designed years before system versioning existed. This project lets the database do that job in one clause.

Developed against **MariaDB 12.3.3** (LTS) on Windows 11, with Python 3.13.

## Quick start

Requirements: MariaDB 12.3 (11.8 or later should also work) and Python 3.11 or later.

1. Create a database and a user (in `mariadb -u root -p`):
   ```sql
   CREATE DATABASE wiki_timemachine CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   CREATE USER 'wiki'@'localhost' IDENTIFIED BY 'choose-a-password';
   GRANT ALL ON wiki_timemachine.* TO 'wiki'@'localhost';
   ```
2. Install and configure:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   copy .env.example .env        # macOS/Linux: cp .env.example .env, then set DB_PASSWORD
   ```
3. Download the revisions, load them, and start the page:
   ```bash
   python -m timemachine.fetch     # about 20 articles, one revision per quarter, saved to data/raw/
   python -m timemachine.load      # recreates the schema and loads the history (about 10 s)
   python -m timemachine.warm      # optional: pre-render every version (about 100 s on 20 cores)
   flask --app timemachine.web run # open http://127.0.0.1:5000
   ```

Try **Pluto** around 24 August 2006, the day it stopped being a planet. Then open **Compare** to see June 2006 against January 2007: "the traditional ninth planet" turns into "the second-largest known dwarf planet".

The Compare page runs a single query that reads `page_content` at two moments and joins it to itself. See `ARTICLE_AT_TWO_MOMENTS` in [`timemachine/queries.py`](timemachine/queries.py).

## Data

The data is the article list in [`articles.txt`](articles.txt), fetched from the MediaWiki API by `timemachine/fetch.py`. Popular articles have tens of thousands of revisions, so the fetcher keeps **the last revision of each quarter** (or each month or year, with `--every`). "As of a date" is therefore exact to within one period. Every revision that is kept has its real Wikipedia timestamp.

## Data model

See [`sql/schema.sql`](sql/schema.sql).

| table | engine / options | holds |
|---|---|---|
| `pages` | InnoDB, not versioned | one row per article: `page_id`, `title` |
| `page_content` | InnoDB, `WITH SYSTEM VERSIONING`, `content MEDIUMTEXT COMPRESSED` | the current text of each article, plus every earlier version, kept by the server, with each version's size in a stored `content_bytes` column |
| `rendered` | InnoDB, not versioned | a cache: each revision turned into readable paragraphs (JSON), keyed by `rev_id` |

Why these tables:
- The title is identity, and the text is what changes. Only the text needs a history, so only `page_content` pays the storage cost of versioning.
- The period columns `sys_start` and `sys_end` are declared explicitly so that queries and the web page can show them. They are `INVISIBLE`, so `SELECT *` still looks like an ordinary table.
- `rendered` is derived data, so it is not versioned. A Wikipedia revision never changes, so a cached rendering never goes stale, and the table survives reloads (`CREATE TABLE IF NOT EXISTS`).

## Where the time goes, and the render cache

Measured before the cache existed (median of 5 runs, warm buffer pool):

| step | Pluto | Barack Obama |
|---|---|---|
| `AS OF` query (read page) | 0.5 ms | 0.7 ms |
| `FOR SYSTEM_TIME ALL` query (timeline) | 1.5 ms | 1.9 ms |
| two-moment self-join (compare page) | 1.3 ms | 2.5 ms |
| **wikitext → paragraphs, in Python** | **416 ms** | **1,138 ms** |

The database was never the bottleneck. `EXPLAIN` shows the `AS OF` query doing a range scan on the primary key, which MariaDB silently extends to `(page_id, sys_end)` on a system-versioned table, so it reads about 5 rows.

The page queries now `LEFT JOIN rendered`, and send the wikitext only when there is no cached rendering (`IF(r.rev_id IS NULL, c.content, NULL)`). On a miss, the page renders the version and stores it. After that:

| page | first view | cached |
|---|---|---|
| Read, Barack Obama 2026 | 1,356 ms | **9.5 ms** |
| Compare, Pluto 2006 → 2007 | 205 ms | **68 ms** (the rest is the diff itself) |

`python -m timemachine.warm` renders everything ahead of time on every core: 1,708 versions in about 100 s, 77 MB of JSON.

## The real design question: whose timestamps?

System versioning stamps a row with the time the statement ran. A naive import therefore records a perfect history of the loading script and nothing about the article. There are four ways to deal with that:

1. **Replay with import-time stamps.** This is simple, but every date is wrong, so it was ruled out.
2. **`replay`: set the session clock to each revision's time**, then `INSERT`/`UPDATE`:
   ```sql
   SET timestamp = UNIX_TIMESTAMP('2006-08-24 14:30:05');
   UPDATE page_content SET content = ... WHERE page_id = ...;
   ```
   The server ends the previous version and starts the new one at that moment.
3. **`insert_history`: write the history rows directly** with their periods spelled out:
   ```sql
   SET system_versioning_insert_history = ON;
   INSERT INTO page_content (page_id, rev_id, content, sys_start, sys_end)
   VALUES (44469, 46359866, '...', '2006-03-31 20:01:40', '2006-06-30 12:49:18'), ...;
   INSERT INTO page_content (page_id, rev_id, content, sys_start)      -- no sys_end: the current version
   VALUES (44469, 1379256428, '...', '2026-10-08 22:22:25');
   ```
4. **`app_period`: an application-time period** (`valid_from`, `valid_to`) holds the Wikipedia dates, protected by `UNIQUE (page_id, wiki_time WITHOUT OVERLAPS)`, on a table that is *also* system-versioned. The system period then means "when we loaded or corrected it", which makes the table bitemporal.

[`bench/time_models.py`](bench/time_models.py) loads the same 1,708 revisions all three ways and asks each one 200 sampled questions of the form "article X as of moment T", from before each article existed up to today. It also probes each method with a bad input. MariaDB 12.3.3, median of 3 loads, 5 passes over the questions, warm buffer pool:

| method | load | size | "X as of T", median | p95 | how you query it |
|---|---|---|---|---|---|
| `replay` | 9.5 s | 233 MB | 0.49 ms | 1.13 ms | `FOR SYSTEM_TIME AS OF` |
| `insert_history` | **4.5 s** | 222 MB | 0.56 ms | 1.39 ms | `FOR SYSTEM_TIME AS OF` |
| `app_period` | 4.6 s | 222 MB | 0.56 ms | 1.35 ms | `WHERE valid_from <= T AND valid_to > T` |

**All three gave the same revision for all 200 questions**, and none of them ever returned two rows. On query speed they are the same within a tenth of a millisecond. The differences are in loading, in what you write, and in what each one lets through:

| bad input (the probe) | what happened |
|---|---|
| `replay`: the clock set *before* the current version started, then `UPDATE` | **Accepted, and the older version silently vanished.** The server cannot store a history row that ends before it starts, so it drops it. Nothing warns you. |
| `insert_history`: two history rows that overlap in time | **Accepted**, and `AS OF` a moment inside the overlap then **returns 2 versions of one page**. MariaDB does not check periods written by hand. |
| `app_period`: the same overlap | **Rejected**: `Duplicate entry ... for key 'no_overlap'`. `WITHOUT OVERLAPS` is a real constraint. |

Other things learned along the way:
- `replay` is half as fast because every revision must be its own transaction (`autocommit`) so that it takes the timestamp just set, and with `innodb_flush_log_at_trx_commit=1` each of those flushes the log. `insert_history` does one transaction per article, so an article's history loads whole or not at all.
- Batching hits `max_allowed_packet`. One `executemany` with Barack Obama's whole history (more than 16 MB) made the server drop the connection, so the loader sends batches of at most 4 MB.
- `replay` and `insert_history` both depend on `secure_timestamp = NO`, the default. According to the MariaDB documentation, a server set to `YES`, which exists to stop exactly this kind of history-writing, refuses both, and only `app_period` still works, because its system period is honest load time. This was not tested here: `secure_timestamp` can only be set when the server starts.
- `app_period` gets corrections right: `UPDATE ... FOR PORTION OF wiki_time FROM '2002-03-01' TO '2002-04-01'` split one version into three, and system versioning kept the row it replaced. But it gives up `FOR SYSTEM_TIME AS OF` for the Wikipedia dates, because there is no `AS OF` for application time, just a `WHERE` on two columns.

**The choice: `insert_history`**, which is what [`timemachine/load.py`](timemachine/load.py) now does by default (`--method replay` is still there). It is twice as fast as `replay`, it is atomic per article, and the table and every query stay the same, with the history in the system period that this project is about. Its weakness, that it trusts the periods you give it, is handled by construction: the loader sorts each article's revisions, drops any whose time is not later than the one before, and makes every version end exactly where the next one starts. [`tests/test_sql.py`](tests/test_sql.py) checks that the stored periods chain with no gaps or overlaps.

`app_period` would be the right choice for a copy that is *edited*, where history gets corrected after the fact and you need to know both when something was true and when you found out, or on a server with `secure_timestamp = YES`. For a read-only archive replayed from a source that already knows its dates, the system period can hold those dates directly.

## How the history grows

### History size per article (the real data)

Query 6 in [`sql/examples.sql`](sql/examples.sql), one stored version per quarter:

| article | versions | current text | history | history ÷ current |
|---|---|---|---|---|
| Barack Obama | 92 | 349 KB | 22.0 MB | 63× |
| Wikipedia | 101 | 334 KB | 16.9 MB | 51× |
| Lionel Messi | 86 | 321 KB | 16.9 MB | 53× |
| Pluto | 101 | 170 KB | 10.2 MB | 60× |
| COVID-19 pandemic | 28 | 375 KB | 9.2 MB | 25× |
| ChatGPT | 17 | 160 KB | 2.5 MB | 16× |
| MariaDB | 69 | 76 KB | 1.7 MB | 23× |
| **all 20 articles** | **1,708** | **4.1 MB** | **203.4 MB** | **≈ 50×** |

**98% of the table is history.** Each version stores the whole article again, so history grows with versions × article size. A long, much-edited article costs 60× its current size after 25 years of quarterly snapshots. Young articles (ChatGPT, COVID-19) have less history so far.

### Query time as the history grows

The real data stops at about 100 versions per article, so [`bench/history_growth.py`](bench/history_growth.py) builds the same table with N versions of each of 5 articles (Pluto, MariaDB, SQL, Tbilisi, Chess). The texts are the articles' real revisions cycled in order, and the dates are spread evenly over 2001–2026. Each N is measured on a plain table and on one with `PARTITION BY SYSTEM_TIME (PARTITION p_history HISTORY, PARTITION p_current CURRENT)`. Times are medians of 150 queries, warm buffer pool:

| versions per article | partitioned | size | `AS OF` now | `AS OF` middle | `AS OF` oldest | rows read, oldest / now | current lookup | `COUNT(*)`, current rows |
|---|---|---|---|---|---|---|---|---|
| 1 | no | 0.1 MB | 0.12 ms | 0.11 ms | 0.12 ms | 1 / 1 | 0.06 ms | 0.05 ms |
| 10 | no | 0.5 MB | 0.11 ms | 0.12 ms | 0.12 ms | 10 / 1 | 0.06 ms | 0.06 ms |
| 100 | no | 40 MB | 0.20 ms | 0.45 ms | 0.64 ms | 100 / 1 | 0.07 ms | 0.10 ms |
| 500 | no | 188 MB | 0.13 ms | 1.51 ms | 2.82 ms | 500 / 1 | 0.08 ms | 0.35 ms |
| 1000 | no | 372 MB | 0.14 ms | 2.79 ms | **5.24 ms** | 1000 / 1 | 0.07 ms | **0.63 ms** |
| 1000 | yes | 373 MB | 0.36 ms | 4.21 ms | 5.75 ms | 1000 / 1 | 0.08 ms | **0.08 ms** |

(All rows are in [`bench/results/history_growth.json`](bench/results/history_growth.json). The small sizes at N = 1 and 10 are because cycling starts with each article's earliest, shortest revisions.)

What the numbers say:
- **The present stays fast however long the history gets.** `AS OF` now and the ordinary lookup read 1 row at every size.
- **The distant past gets slower in a straight line**, from 0.12 ms to 5.2 ms at 1,000 versions, because it reads every version *newer* than the moment asked about. `ANALYZE FORMAT=JSON` shows `r_rows` equal to N for the oldest moment and 1 for now. The reason is that MariaDB silently adds `sys_end` to the primary key of a system-versioned table, making it `(page_id, sys_end)`, and `AS OF T` becomes a range scan over `sys_end > T` that filters on `sys_start <= T`. The older T is, the more of that range it walks.
- **Partitioning by `SYSTEM_TIME` does not speed up `AS OF`.** It reads both partitions, and was slightly slower here. What it does is keep *current-data* scans constant: `COUNT(*)` stays at 0.08 ms with 1,000 versions per article instead of 0.63 ms, because the history partition is pruned. It also turns history clean-up into dropping a partition. It is worth it for a table where most queries are about the present.
- At this project's real size (about 100 versions per article) the oldest-moment cost is about 0.6 ms, so the history adds no visible cost to the page.

### Compressing the history

Since history is 98% of the table and every version repeats most of the article, it should compress well. MariaDB has three ways to do it. [`bench/compression.py`](bench/compression.py) loads the same data into `page_content` with each one, using the same loader as `timemachine.load`. It forces InnoDB to write to disk with `FLUSH TABLES ... FOR EXPORT` (which needs `GRANT RELOAD`), then reads the size of the `.ibd` file. MariaDB 12.3.3, zlib level 6, median of 3 loads, query times are medians of 15 runs on a warm buffer pool:

| variant | on disk | vs none | load | `AS OF`, reading the text | timeline, `content_bytes` | timeline, `LENGTH(content)` | read every version |
|---|---|---|---|---|---|---|---|
| none | 224 MB | 100% | 2.3 s | 0.20 ms | 1.53 ms | 1.72 ms | 16 ms |
| **`content MEDIUMTEXT COMPRESSED`** | **88 MB** | **39%** | 8.3 s | 1.11 ms | **0.42 ms** | 54 ms | 546 ms |
| `PAGE_COMPRESSED=1` | 221 MB | 99% | 5.1 s | 0.19 ms | 1.45 ms | 1.71 ms | 15 ms |
| `ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8` | 80 MB | 36% | 8.2 s | 5.68 ms | 67.7 ms | 68.5 ms | 669 ms |

(Raw numbers: [`bench/results/compression.json`](bench/results/compression.json).)

What the numbers say:
- **Column compression is MariaDB-specific and is the one this project uses.** It takes the table from 224 MB to 88 MB. The price is paid where the text is actually read: about 1 ms more for each version that is decompressed, and 35× more for anything that reads every version.
- **The first run of this benchmark showed that price in the wrong place.** The timeline computed `LENGTH(content)` for every version, which on a compressed column means decompressing all of them: 54 ms instead of 1.7 ms, on every page view. The fix is in the schema: `content_bytes INT UNSIGNED AS (LENGTH(content)) STORED` works out the size once, when the row is written, and the timeline reads that column instead. It is now *faster* than on the uncompressed table (0.42 ms against 1.53 ms), probably because the compressed rows are smaller.
- In the app, a version's text is only read when it is not in the `rendered` cache yet, so the extra 1 ms almost never happens. Page views measured after switching: 3–5 ms for the Read page.
- **`ROW_FORMAT=COMPRESSED` is smallest but slowest.** It compresses whole 8 KB pages, so even reading `content_bytes` means decompressing pages: 68 ms for the timeline. It is also the older InnoDB format.
- **Page compression saved almost nothing here (1%).** It compresses each 16 KB page and gives the freed space back by punching holes in the file. On this machine, Windows 11 with NTFS, the file barely shrank. A likely explanation, not tested here, is that NTFS releases the space of a sparse file in larger units than one InnoDB page, so a hole in a single page cannot be given back. On Linux filesystems with 4 KB blocks the result should be different. Run the benchmark there before you rely on this row.
- A side observation: loaded with `insert_history`, the uncompressed table is 224 MB. Loaded with `replay`, the same data took 272 MB, which looks like the cost of 1,688 `UPDATE`s moving rows around. Measured once each, and not investigated further.

## Example queries

See [`sql/examples.sql`](sql/examples.sql) for queries you can paste into the client: `AS OF`, `BETWEEN`, `ALL`, and the history-size breakdowns. All of the SQL the web app runs is in [`timemachine/queries.py`](timemachine/queries.py).

One gotcha found by running the file: on MariaDB 12.3.3, `FOR SYSTEM_TIME BETWEEN TIMESTAMP '2020-01-01' AND ...` fails with `ERROR 1525 Incorrect DATETIME value`. Give the literal a time part (`'2020-01-01 00:00:00'`).

## Tests

```bash
python -m pytest
```

[`tests/test_sql.py`](tests/test_sql.py) runs against the loaded database. It checks that:
- every statement in `sql/examples.sql` runs
- Pluto reads "ninth planet" before 24 August 2006 and "dwarf planet" after
- an article has no version before it existed
- each article's periods chain with no gaps or overlaps
- there is exactly one current version per article
- the history carries Wikipedia's dates, not the load date
- the web pages render

The tests are skipped when there is no `.env` or no database.

## Project layout

```
articles.txt              the fixed set of articles
sql/schema.sql            the two tables (the important file)
sql/examples.sql          queries to paste into the mariadb client
timemachine/fetch.py      MediaWiki API -> data/raw/*.json
timemachine/load.py       data/raw -> MariaDB, history with real timestamps (insert_history, or --method replay)
timemachine/queries.py    every SQL statement the web app uses
timemachine/render.py     wikitext -> readable paragraphs, and the paragraph/word diff
timemachine/warm.py       pre-renders every version into the `rendered` cache
timemachine/web.py        Flask app: / (read as of a moment) and /diff (compare two moments)
timemachine/templates/    the pages
timemachine/static/       stylesheet and the timeline/animation script
bench/time_models.py      replay vs insert_history vs application-time period
bench/history_growth.py   query time and size as the history grows, with and without partitioning
bench/compression.py      column, page and row compression compared (needs GRANT RELOAD, see the file)
bench/results/            the JSON each benchmark writes
tests/test_sql.py         checks against the loaded database (python -m pytest)
```

## Roadmap

- [x] Show a diff between two chosen moments
- [x] Measure how the versioned table grows (size per article, query time vs versions)
- [x] `PARTITION BY SYSTEM_TIME`, measured
- [x] `replay`, `insert_history` and an application-time period, compared and one chosen
- [x] Compression of the history: `COMPRESSED` column, page and row compression measured, column compression applied
- [ ] An index that keeps old-moment `AS OF` flat (for example on `sys_start`), measured
- [ ] Where history in the database stops being the right answer (privacy deletion with `DELETE HISTORY`, storage cost)

## Team

Nikolozi Barkalaia (solo)

## License

[MIT](LICENSE)
