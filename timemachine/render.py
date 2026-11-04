"""Turning stored wikitext into something readable, and comparing two versions.

Nothing in here touches the database: it works on the text that the
FOR SYSTEM_TIME queries return. The results are cached in the `rendered`
table by timemachine.web and timemachine.warm.
"""

import re
from difflib import SequenceMatcher

import mwparserfromhell

# Links that are not part of the prose: images, categories, and old-style
# interlanguage links such as [[de:Pluto]].
NON_TEXT_LINK = re.compile(r"^\s*(file|image|category|media|[a-z]{2,3}(-[a-z]+)?)\s*:", re.IGNORECASE)
HEADING_MARK = "§§ "


def to_paragraphs(wikitext: str) -> list[dict]:
    """Wikitext -> [{"heading": bool, "text": str}, ...]. Not a full renderer: just enough
    to read the article. Templates such as infoboxes, references and images are dropped.
    Slow (0.4 to 1 s for a long article), which is why the result is cached."""
    wikitext = wikitext.replace("\r\n", "\n")  # revisions from 2001-2004 use Windows line endings
    wikitext = re.sub(r"^'{2,}\s*$", "", wikitext, flags=re.MULTILINE)  # a lone ''' would turn the rest bold
    wikitext = re.sub(r"^:\s*''.*$", "", wikitext, flags=re.MULTILINE)  # old-style hatnotes (": ''For other uses, see ...''")

    code = mwparserfromhell.parse(wikitext)
    unwanted = [link for link in code.filter_wikilinks() if NON_TEXT_LINK.match(str(link.title))]
    unwanted += code.filter_tags(matches=lambda t: str(t.tag).lower() in ("ref", "gallery", "table"))
    for node in unwanted:
        try:
            code.remove(node)
        except ValueError:
            pass  # already gone with an enclosing node
    for heading in code.filter_headings():
        code.replace(heading, f"\n\n{HEADING_MARK}{heading.title.strip_code().strip()}\n\n")

    text = code.strip_code(normalize=True, collapse=True)
    text = re.sub(r"'{2,}", "", text)  # quotes left over from any other broken markup

    out = []
    for p in (p.strip() for p in text.split("\n\n")):
        if p.startswith(HEADING_MARK):
            out.append({"heading": True, "text": p[len(HEADING_MARK):]})
        elif p:
            out.append({"heading": False, "text": p})
    # Drop headings with nothing under them (their section was all images or tables)
    return [p for i, p in enumerate(out) if not p["heading"] or (i + 1 < len(out) and not out[i + 1]["heading"])]


def _word_diff(old: str, new: str) -> list[tuple[str, str]]:
    """[(kind, text)] with kind in same / del / ins, at word level."""
    a, b = re.findall(r"\S+|\s+", old), re.findall(r"\S+|\s+", new)
    parts = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            parts.append(("same", "".join(a[i1:i2])))
        if op in ("delete", "replace"):
            parts.append(("del", "".join(a[i1:i2])))
        if op in ("insert", "replace"):
            parts.append(("ins", "".join(b[j1:j2])))
    return parts


def diff(old: list[dict], new: list[dict]) -> dict:
    """Paragraph-level diff of two to_paragraphs() results, with word-level detail
    inside changed paragraphs.

    Returns {"blocks": [...], "added": n, "removed": n, "changed": n}, where each block is
      {"kind": "same", "count": n}                    a run of unchanged paragraphs
      {"kind": "added" | "removed", "para": {...}}    a whole paragraph
      {"kind": "changed", "heading": bool, "parts": [(kind, text), ...]}
    """
    a, b =[p["text"] for p in old], [p["text"] for p in new]
    blocks, stats = [], {"added": 0, "removed": 0, "changed": 0}

    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            blocks.append({"kind": "same", "count": i2 - i1})
            continue
        olds, news = list(old[i1:i2]), list(new[j1:j2])
        # Pair paragraphs up in order: an edited paragraph shows as a word diff,
        # anything left over on one side was removed or added whole.
        while olds and news:
            o, n = olds.pop(0), news.pop(0)
            blocks.append({"kind": "changed", "heading": n["heading"], "parts": _word_diff(o["text"], n["text"])})
            stats["changed"] += 1
        for p in olds:
            blocks.append({"kind": "removed", "para": p})
            stats["removed"] += 1
        for p in news:
            blocks.append({"kind": "added", "para": p})
            stats["added"] += 1

    return {"blocks": blocks, **stats}
