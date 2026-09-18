"""Discover every article through the complete archive, not the limited feed."""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .fetch import Fetcher

BASE = "https://yuc.wiki/"


def article_url(href: str, base: str = BASE) -> str | None:
    # Old cards contain literal href="链接" placeholders. Only root-relative or
    # absolute site links are navigation evidence; retain every href in the JSON.
    if not href.startswith(("/", "https://", "http://")):
        return None
    url = urlsplit(urljoin(base, href))
    if url.hostname != "yuc.wiki" or url.scheme not in {"http", "https"} or url.query:
        return None
    path = unquote(url.path).strip("/")
    if path.endswith("/index.html"):
        path = path[:-11].strip("/")
    if not path or path == "index.html" or "." in path or "\\" in path:
        return None
    if path.split("/")[0] in {"archives", "tags", "categories", "page", "css", "js", "images", "lib"}:
        return None
    return BASE + path + "/"


def page_id(url: str) -> str:
    path = unquote(urlsplit(url).path).strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", path):
        raise ValueError(f"unsupported article slug (requires review): {path}")
    return path.replace("/", "__")


def page_kind(slug: str) -> str:
    if re.fullmatch(r"20\d{2}(01|04|07|10)", slug):
        return "quarter"
    return {"index": "index", "new": "upcoming", "sp": "special", "movie": "movie"}.get(slug, "article")


def linked_articles(payload: bytes, url: str) -> set[str]:
    soup = BeautifulSoup(payload.decode("utf-8"), "html.parser")
    return {target for link in soup.select(".post-body a[href], .post-nav a[href]")
            if (target := article_url(link["href"], url))}


def discover(fetcher: Fetcher) -> tuple[list[str], dict[str, tuple[bytes, dict]]]:
    evidence: dict[str, tuple[bytes, dict]] = {}
    homepage = fetcher.get(BASE)
    evidence["homepage.html"] = homepage
    home = BeautifulSoup(homepage[0], "html.parser")
    candidates = {u for a in home.select(".post-title a[href]") if (u := article_url(a["href"]))}
    seen = set()
    pending = [BASE + "archives/"]
    archive_urls = set()
    claimed_count = None
    while pending:
        url = pending.pop(0)
        if url in seen:
            continue
        if len(seen) >= 100:
            raise ValueError("archive pagination exceeded 100 pages")
        seen.add(url)
        payload = fetcher.get(url)
        name = "archives" + ("-" + str(len(seen)) if len(seen) > 1 else "") + ".html"
        evidence[name] = payload
        soup = BeautifulSoup(payload[0], "html.parser")
        if not soup.select_one(".posts-collapse"):
            raise ValueError(f"archive layout changed: {url}")
        for link in soup.select(".posts-collapse .post-title a[href]"):
            normalized = article_url(link["href"])
            if normalized:
                archive_urls.add(normalized)
        match = re.search(r"目前共计\s*(\d+)\s*篇", soup.get_text(" ", strip=True))
        if match:
            number = int(match[1])
            if claimed_count is not None and claimed_count != number:
                raise ValueError("archive article count changed during discovery")
            claimed_count = number
        for link in soup.select(".pagination a[href]"):
            target = urlsplit(urljoin(url, link["href"]))
            if target.hostname == "yuc.wiki" and re.fullmatch(r"/archives/(?:page/\d+/)?", target.path):
                next_url = BASE.rstrip("/") + target.path
                if next_url not in seen and next_url not in pending:
                    pending.append(next_url)
    if claimed_count is None or len(archive_urls) != claimed_count:
        raise ValueError(f"incomplete archive: expected {claimed_count}, found {len(archive_urls)}")
    candidates.update(archive_urls)
    if not candidates:
        raise ValueError("no articles discovered")
    return sorted(candidates), evidence
