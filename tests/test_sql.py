"""Checks that run against the loaded database.

    python -m pytest

Needs .env and a loaded database (python -m timemachine.load). Skipped otherwise.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

try:
    from timemachine.db import connect
    _conn = connect(autocommit=True)
except BaseException as err:  # connect() raises SystemExit without a password
    pytest.skip(f"no database to test against: {err}", allow_module_level=True)


def statements(path: Path) -> list[str]:
    """Split a .sql file the way the mariadb client would see it: on ';', comment lines dropped."""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


@pytest.fixture
def cur():
    return _conn.cursor()


@pytest.mark.parametrize("sql", statements(ROOT / "sql" / "examples.sql"), ids=lambda s: s.splitlines()[0][:60])
def test_every_example_query_runs(cur, sql):
    cur.execute(sql)
    if cur.description:
        cur.fetchall()


AS_OF = """
SELECT c.content FROM page_content FOR SYSTEM_TIME AS OF TIMESTAMP ? AS c
JOIN pages p USING (page_id) WHERE p.title = ?
"""


@pytest.mark.parametrize("moment, expected", [
    ("2006-06-01 00:00:00", "ninth planet"),
    ("2007-01-01 00:00:00", "dwarf planet"),
])
def test_pluto_before_and_after_2006(cur, moment, expected):
    cur.execute(AS_OF, (moment, "Pluto"))
    (content,) = cur.fetchone()
    assert expected in content


def test_article_did_not_exist_before_its_first_version(cur):
    cur.execute(AS_OF, ("2015-01-01 00:00:00", "ChatGPT"))
    assert cur.fetchone() is None


def test_versions_chain_without_gaps_or_overlaps(cur):
    # Every version ends exactly when the next one of the same article starts.
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT sys_end, LEAD(sys_start) OVER (PARTITION BY page_id ORDER BY sys_start) AS next_start
            FROM page_content FOR SYSTEM_TIME ALL
        ) v
        WHERE next_start IS NOT NULL AND next_start <> sys_end
    """)
    assert cur.fetchone()[0] == 0


def test_exactly_one_current_version_per_article(cur):
    cur.execute("SELECT (SELECT COUNT(*) FROM pages), (SELECT COUNT(*) FROM page_content)")
    pages, current = cur.fetchone()
    assert pages == current > 0


def test_history_carries_wikipedia_dates_not_load_dates(cur):
    cur.execute("SELECT MIN(sys_start) FROM page_content FOR SYSTEM_TIME ALL")
    assert cur.fetchone()[0].year < 2005


@pytest.mark.parametrize("url", [
    "/?title=Pluto&at=2006-06-01T00:00",
    "/diff?title=Pluto&from=2006-06-01T00:00&to=2007-01-01T00:00",
    "/diff?title=ChatGPT&from=2015-01-01T00:00",
])
def test_pages_render(url):
    from timemachine.web import app
    assert app.test_client().get(url).status_code == 200
