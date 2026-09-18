"""Extract published facts without guessing dates, identities, or credits."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction, Tag

from . import PARSER_VERSION, SCHEMA_VERSION


def text(node: Tag | None) -> str:
    """Keep source line boundaries, including BRs, without splitting inline text."""
    if node is None:
        return ""
    parts: list[str] = []
    blocks = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "hr"}

    def visit(value: Any) -> None:
        if isinstance(value, (Comment, Declaration, Doctype, ProcessingInstruction)):
            return
        if isinstance(value, NavigableString):
            parts.append(str(value))
        elif isinstance(value, Tag):
            if value.name in {"script", "style"}:
                return
            if value.name == "br":
                parts.append("\n")
                return
            if value.name in blocks:
                parts.append("\n")
            for child in value.children:
                visit(child)
            if value.name in blocks:
                parts.append("\n")
            elif value.name in {"td", "th"}:
                parts.append("\t")

    visit(node)
    return "\n".join(
        re.sub(r"[\t \u00a0\u3000]+", " ", line).strip()
        for line in "".join(parts).splitlines()
        if line.strip()
    )


def class_has(node: Tag, prefix: str) -> bool:
    return any(c.startswith(prefix) for c in node.get("class", []))


def first(node: Tag, prefix: str) -> Tag | None:
    return node.find(lambda t: isinstance(t, Tag) and class_has(t, prefix))


def absolute_url(value: str | None, base: str) -> str | None:
    if not value:
        return None
    result = urljoin(base, value.strip())
    return result if urlsplit(result).scheme in {"https", "http"} else None


def links(node: Tag, base: str) -> list[dict]:
    result = []
    for link in node.find_all("a", href=True):
        result.append({
            "text": text(link),
            "href": link["href"],
            "url": absolute_url(link["href"], base),
            "title": link.get("title"),
            "images": images(link, base),
        })
    return result


def images(node: Tag, base: str) -> list[dict]:
    return [{
        "url": absolute_url(img.get("data-src") or img.get("src"), base),
        "src": img.get("src"),
        "dataSrc": img.get("data-src"),
        "srcset": img.get("srcset"),
        "alt": img.get("alt"),
        "title": img.get("title"),
    } for img in node.find_all("img")]


def field(node: Tag | None) -> dict:
    value = text(node)
    return {"text": value or None, "lines": value.splitlines()}


def staff(node: Tag | None) -> dict:
    raw = field(node)
    credits: list[dict] = []
    for line in raw["lines"]:
        # A source line can contain several roles, e.g. 插画：A / 漫画：B.
        segments = re.split(r"[/／]\s*(?=[^：: /／]{1,12}[：:])", line)
        for segment in segments:
            match = re.match(r"\s*([^：:]{1,30})[：:]\s*(.*)", segment)
            if match:
                credits.append({"role": match[1].strip(), "namesText": match[2].strip()})
            elif credits:
                credits[-1]["namesText"] += "\n" + segment.strip()
            else:
                credits.append({"role": None, "namesText": segment.strip()})
    return {**raw, "credits": credits}


def platform_links(node: Tag | None, base: str) -> list[dict]:
    if node is None:
        return []
    result = []
    for item in links(node, base):
        host = urlsplit(item["url"] or "").hostname or ""
        result.append({
            "label": item["text"] or None,
            "url": item["url"],
            "href": item["href"],
            "host": host or None,
            "images": item["images"],
        })
    return result


def table_cells(table: Tag, base: str) -> list[list[dict]]:
    """Keep every source cell so unfamiliar fields remain accessible in JSON."""
    return [[{
        "tag": cell.name,
        "classes": cell.get("class", []),
        "rowspan": cell.get("rowspan"),
        "colspan": cell.get("colspan"),
        "text": text(cell),
        "html": str(cell),
        "links": links(cell, base),
        "images": images(cell, base),
    } for cell in row.find_all(["td", "th"], recursive=False)]
        for row in table.find_all("tr") if row.find_parent("table") is table]


def _source_marker(table: Tag) -> str | None:
    # Comments such as #C19 are source locators, not stable anime identifiers.
    for previous in table.previous_elements:
        if isinstance(previous, Tag) and previous.name == "table":
            break
        if isinstance(previous, Comment) and re.fullmatch(r"\s*#[A-Z]\d+\s*", str(previous)):
            return str(previous).strip()
    return None


def _preceding_cover(table: Tag, base: str) -> dict | None:
    for previous in table.previous_elements:
        if isinstance(previous, Tag):
            if previous.name == "table":
                break
            if previous.name == "img":
                return {
                    "url": absolute_url(previous.get("data-src") or previous.get("src"), base),
                    "src": previous.get("src"), "dataSrc": previous.get("data-src"),
                    "alt": previous.get("alt"), "title": previous.get("title"),
                }
    return None


def introduction(table: Tag, base: str, page_id: str, index: int) -> dict:
    marker = _source_marker(table)
    chinese = first(table, "title_cn")
    original = first(table, "title_jp")
    type_node = table.find(lambda t: isinstance(t, Tag) and any(
        re.match(r"type_[a-z](?:_|$)", c) for c in t.get("class", [])))
    tags = field(first(table, "type_tag"))
    staff_field = staff(first(table, "staff"))
    cast = field(first(table, "cast"))
    link_area = first(table, "link_a")
    platform_area = first(table, "link_b")
    card_links = links(table, base)
    return {
        "recordId": f"{page_id}:introduction:{index}",
        "sourceMarker": marker,
        "titles": {"chinese": field(chinese), "original": field(original)},
        "adaptation": text(type_node) or None,
        "tags": {**tags, "values": [v.strip() for v in re.split(r"[/／\n]", tags["text"] or "") if v.strip()]},
        "staff": staff_field,
        "cast": cast,
        "studios": [c["namesText"] for c in staff_field["credits"] if c["role"] == "动画制作"],
        "cover": _preceding_cover(table, base),
        "officialSites": [l for l in card_links if "官网" in l["text"]],
        "promotionalVideos": [l for l in card_links if re.search(r"\bPV\b|预告|宣传", l["text"], re.I)],
        "broadcast": field(first(table, "broadcast_r") or first(table, "broadcast")),
        "broadcastNotes": field(first(table, "broadcast_ex")),
        "linkAreaText": text(link_area) or None,
        "platforms": platform_links(platform_area, base),
        "links": card_links,
        "raw": {"text": text(table), "html": str(table), "cells": table_cells(table, base)},
    }


def lineup(table: Tag, base: str, page_id: str, index: int) -> dict:
    title = first(table, "date_title")
    group = None
    for previous in table.previous_elements:
        if isinstance(previous, Tag) and previous.name == "td" and {"date", "date2"}.intersection(previous.get("class", [])):
            group = text(previous)
            break
    meta = None
    for previous in table.previous_elements:
        if isinstance(previous, Tag):
            if previous.name == "table":
                break
            if class_has(previous, "div_date"):
                meta = previous
                break
    extra = first(table, "tr_area_ex")
    all_notes = []
    for region in [meta, extra]:
        if region is not None:
            all_notes.extend(text(p) for p in region.find_all("p") if text(p))
    # Preserve all strings and use semantic patterns instead of positional p[0]/p[1].
    episodes = [n for n in all_notes if re.search(r"(?:全|共|预计)?\s*\d+\s*[话話集]", n)]
    states = [n for n in all_notes if n not in episodes]
    return {
        "recordId": f"{page_id}:lineup:{index}",
        "title": field(title), "group": group,
        "statusOrTime": states, "episodeNotes": episodes,
        "notes": all_notes, "extraNotes": field(extra),
        "cover": _preceding_cover(table, base),
        "platforms": [p for row in table.find_all("tr", class_="tr_area") for p in platform_links(row, base)],
        "raw": {"text": text(meta) + "\n" + text(table), "html": (str(meta) if meta else "") + str(table), "cells": table_cells(table, base)},
    }


def compact_card(node: Tag, base: str, page_id: str, category: str, index: int) -> dict:
    prefixes = {"preview": "future", "special": "sp", "movie": "movie"}
    prefix = prefixes[category]
    title = first(node, prefix + "_title")
    cover_images = images(node, base)
    return {
        "recordId": f"{page_id}:{category}:{index}",
        "title": field(title),
        "format": field(first(node, "future_type") if category == "preview" else first(node, "type-")),
        "release": field(first(node, "future_date") if category == "preview" else first(node, prefix + "_release")),
        "boxOffice": field(first(node, "movie_box")),
        "images": cover_images,
        "links": links(node, base),
        "raw": {"text": text(node), "html": str(node)},
    }


def parse_page(html_bytes: bytes, source: dict) -> dict:
    soup = BeautifulSoup(html_bytes.decode("utf-8"), "html.parser")
    body = soup.select_one('.post-body[itemprop="articleBody"]') or soup.select_one(".post-body")
    if body is None or not text(body):
        raise ValueError(f"missing article body: {source['url']}")
    title_node = soup.select_one(".post-title")
    og_title = soup.find("meta", property="og:title")
    title = text(title_node) or (og_title.get("content") if og_title else None)
    if not title:
        raise ValueError(f"missing title: {source['url']}")
    # Retain the entire article in both text and HTML. Structured cards supplement it.
    tables = body.find_all("table")
    intro_tables = [t for t in tables if first(t, "title_cn") is not None]
    lineup_tables = [t for t in tables if first(t, "date_title") is not None]
    previews = []
    for i, node in enumerate(body.find_all(lambda t: isinstance(t, Tag) and "future_div" in t.get("class", [])), 1):
        previews.append(compact_card(node.parent, source["url"], source["pageId"], "preview", i))
    specials, movies = [], []
    for node in body.find_all(lambda t: isinstance(t, Tag) and "div_sp" in t.get("class", [])):
        wrapper = node.parent
        category = "movie" if first(wrapper, "movie_title") is not None else "special"
        target = movies if category == "movie" else specials
        target.append(compact_card(wrapper, source["url"], source["pageId"], category, len(target) + 1))
    # Repeated og:image/article:tag and unnamed metadata are all source data.
    meta = [dict(m.attrs) for m in soup.find_all("meta")]
    claimed = []
    for node in body.find_all(class_=lambda c: c and c.startswith("intro")):
        for count in re.findall(r"共收录\s*(\d+)", text(node).replace("\n", " ")):
            claimed.append(int(count))
    warnings = []
    if claimed and len(intro_tables) not in claimed:
        warnings.append({"code": "introduction_count_mismatch", "claimed": claimed, "parsed": len(intro_tables)})
    return {
        "schemaVersion": SCHEMA_VERSION,
        "parserVersion": PARSER_VERSION,
        "source": {**source, "htmlSha256": hashlib.sha256(html_bytes).hexdigest()},
        "title": title,
        "metadata": meta,
        "content": {"text": text(body), "html": str(body), "links": links(body, source["url"]), "images": images(body, source["url"])},
        "introductions": [introduction(t, source["url"], source["pageId"], i) for i, t in enumerate(intro_tables, 1)],
        "lineup": [lineup(t, source["url"], source["pageId"], i) for i, t in enumerate(lineup_tables, 1)],
        "previews": previews,
        "specials": specials,
        "movies": movies,
        "tables": [{"index": i, "classes": t.get("class", []), "cells": table_cells(t, source["url"])} for i, t in enumerate(tables, 1)
                   if t not in intro_tables and t not in lineup_tables],
        "quality": {"introductionCount": len(intro_tables), "lineupCount": len(lineup_tables), "previewCount": len(previews),
                    "specialCount": len(specials), "movieCount": len(movies),
                    "claimedIntroductionCounts": claimed, "warnings": warnings},
    }
