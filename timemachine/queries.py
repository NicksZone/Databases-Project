"""Every SQL statement the web app runs, in one place.

The point of the project is that these stay this short: the history lives in
the table definition (sql/schema.sql), not in the queries.

The page queries also LEFT JOIN the `rendered` cache. When a version is
already rendered, its wikitext is not sent at all (content comes back NULL),
when it is not, the wikitext comes back so Python can render and store it.
"""

LIST_ARTICLES = """
SELECT title FROM pages ORDER BY title
"""

# The article as it stood at a moment in time.
ARTICLE_AS_OF = """
SELECT c.rev_id, c.editor, c.edit_comment, c.sys_start, c.sys_end,
       r.paragraphs,
       IF(r.rev_id IS NULL, c.content, NULL) AS content
FROM page_content FOR SYSTEM_TIME AS OF TIMESTAMP ? AS c
JOIN pages p USING (page_id)
LEFT JOIN rendered r ON r.rev_id = c.rev_id
WHERE p.title = ?
"""

# The same table read at two moments and joined to itself: the diff page.
# LEFT JOINs, so an article that did not exist yet at the first moment still
# comes back (with NULLs on that side).
ARTICLE_AT_TWO_MOMENTS = """
SELECT old.rev_id AS old_rev_id, old.sys_start AS old_start,
       ro.paragraphs AS old_paragraphs, IF(ro.rev_id IS NULL, old.content, NULL) AS old_content,
       new.rev_id AS new_rev_id, new.sys_start AS new_start,
       rn.paragraphs AS new_paragraphs, IF(rn.rev_id IS NULL, new.content, NULL) AS new_content
FROM pages p
LEFT JOIN page_content FOR SYSTEM_TIME AS OF TIMESTAMP ? AS old ON old.page_id = p.page_id
LEFT JOIN page_content FOR SYSTEM_TIME AS OF TIMESTAMP ? AS new ON new.page_id = p.page_id
LEFT JOIN rendered ro ON ro.rev_id = old.rev_id
LEFT JOIN rendered rn ON rn.rev_id = new.rev_id
WHERE p.title = ?
"""

# Every stored version of one article, for the timeline.
ARTICLE_VERSIONS = """
SELECT c.rev_id, c.editor, c.sys_start, c.content_bytes AS bytes
FROM page_content FOR SYSTEM_TIME ALL AS c
JOIN pages p USING (page_id)
WHERE p.title = ?
ORDER BY c.sys_start
"""

# Store a rendered version. IGNORE: two requests may render the same version at once.
SAVE_RENDERED = """
INSERT IGNORE INTO rendered (rev_id, paragraphs) VALUES (?, ?)
"""

# Every stored version that has not been rendered yet, for timemachine.warm.
UNRENDERED_VERSIONS = """
SELECT c.rev_id, c.content
FROM page_content FOR SYSTEM_TIME ALL AS c
LEFT JOIN rendered r ON r.rev_id = c.rev_id
WHERE r.rev_id IS NULL
"""
