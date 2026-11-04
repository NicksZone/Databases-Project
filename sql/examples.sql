-- Example queries to paste into the mariadb client after loading the data.
--   mariadb -u wiki -p wiki_timemachine
-- All times are UTC. Run this first so the timestamps below mean UTC:
SET time_zone = '+00:00';

-- 1. The article as it is now. An ordinary query only sees current rows.
SELECT p.title, c.rev_id, c.editor, LEFT(c.content, 300) AS start_of_text
FROM pages p JOIN page_content c USING (page_id)
WHERE p.title = 'Pluto';

-- 2. The article as it stood on a given day. One clause, no revision table.
--    Pluto lost its planet status on 2006-08-24: compare a date before and after.
SELECT c.rev_id, c.sys_start AS version_from, LEFT(c.content, 300) AS start_of_text
FROM page_content FOR SYSTEM_TIME AS OF TIMESTAMP '2006-06-01 00:00:00' AS c
JOIN pages p USING (page_id)
WHERE p.title = 'Pluto';

SELECT c.rev_id, c.sys_start AS version_from, LEFT(c.content, 300) AS start_of_text
FROM page_content FOR SYSTEM_TIME AS OF TIMESTAMP '2007-01-01 00:00:00' AS c
JOIN pages p USING (page_id)
WHERE p.title = 'Pluto';

-- 3. Every stored version of one article, oldest first.
SELECT c.rev_id, c.editor, c.sys_start, c.sys_end, c.content_bytes AS bytes
FROM page_content FOR SYSTEM_TIME ALL AS c
JOIN pages p USING (page_id)
WHERE p.title = 'Pluto'
ORDER BY c.sys_start;

-- 4. Versions that were live at some point during 2020.
--    Give both ends a time part: MariaDB 12.3 rejects TIMESTAMP '2020-01-01' here
--    with ERROR 1525 (Incorrect DATETIME value). tests/test_sql.py runs this file.
SELECT p.title, COUNT(*) AS versions_live_in_2020
FROM page_content FOR SYSTEM_TIME BETWEEN TIMESTAMP '2020-01-01 00:00:00' AND TIMESTAMP '2020-12-31 23:59:59' AS c
JOIN pages p USING (page_id)
GROUP BY p.title
ORDER BY versions_live_in_2020 DESC;

-- 5. How much is history and how much is current data.
--    content_bytes is stored with each row, so these size queries never decompress the text.
SELECT
    SUM(sys_end >  NOW())                          AS current_rows,  -- current rows end at the maximum timestamp
    SUM(sys_end <= NOW())                          AS history_rows,
    ROUND(SUM(content_bytes) / 1024 / 1024, 1)   AS text_mb
FROM page_content FOR SYSTEM_TIME ALL;

-- 6. History size per article: what the current text costs, and what its past costs.
SELECT p.title,
       COUNT(*)                                                          AS versions,
       ROUND(SUM(IF(c.sys_end >  NOW(), c.content_bytes, 0)) / 1024)  AS current_kb,
       ROUND(SUM(IF(c.sys_end <= NOW(), c.content_bytes, 0)) / 1024)  AS history_kb,
       ROUND(SUM(IF(c.sys_end <= NOW(), c.content_bytes, 0))
           / SUM(IF(c.sys_end >  NOW(), c.content_bytes, 0)), 1)      AS history_x_current
FROM page_content FOR SYSTEM_TIME ALL AS c
JOIN pages p USING (page_id)
GROUP BY p.title
ORDER BY history_kb DESC;
