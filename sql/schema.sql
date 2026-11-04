-- Wikipedia time machine: schema
-- Developed against MariaDB 12.3.3.
--
-- Two tables:
--   pages         one row per article, plain InnoDB, no history kept
--   page_content  the article text, WITH SYSTEM VERSIONING, so the server
--                 keeps every earlier version and FOR SYSTEM_TIME can read them
--
-- CREATE OR REPLACE makes the script safe to re-run: running it again starts
-- from an empty database without a DROP TABLE IF EXISTS step.
-- Note: no semicolons inside comments, the loader splits this file on them.

CREATE OR REPLACE TABLE pages (
    page_id   INT UNSIGNED NOT NULL PRIMARY KEY,  -- Wikipedia's own page id
    title     VARCHAR(255) NOT NULL,
    UNIQUE KEY uq_pages_title (title)
) ENGINE=InnoDB;

CREATE OR REPLACE TABLE page_content (
    page_id       INT UNSIGNED NOT NULL PRIMARY KEY,
    rev_id        INT UNSIGNED NOT NULL,          -- Wikipedia revision id of this version
    editor        VARCHAR(255) NULL,              -- NULL when Wikipedia hides the user name
    edit_comment  TEXT NULL,
    content       MEDIUMTEXT NOT NULL,            -- raw wikitext

    -- The system period. The server fills these in, the application never does.
    -- INVISIBLE keeps them out of SELECT * until you name them.
    sys_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
    sys_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
    PERIOD FOR SYSTEM_TIME (sys_start, sys_end)
) ENGINE=InnoDB
  WITH SYSTEM VERSIONING;

-- A cache of each revision turned into readable paragraphs. Turning wikitext
-- into text takes Python 0.4 to 1 second per version, the queries above take
-- about a millisecond, so the web page stores the result the first time.
-- Keyed by rev_id, because a Wikipedia revision never changes. Not versioned:
-- it is derived data, and history of a cache would mean nothing.
-- IF NOT EXISTS rather than OR REPLACE: a reload keeps the cache, since the
-- same revisions render the same way. python -m timemachine.warm --rebuild
-- empties it when the rendering code changes.
CREATE TABLE IF NOT EXISTS rendered (
    rev_id      INT UNSIGNED NOT NULL PRIMARY KEY,
    paragraphs  JSON NOT NULL,                    -- [{"heading": bool, "text": str}, ...]
    rendered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
