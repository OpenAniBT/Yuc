"""Offline validation of source snapshots, discovery coverage and health state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

from . import PARSER_VERSION, SCHEMA_VERSION
from .discovery import BASE, discover, linked_articles, page_id, page_kind
from .parser import class_has, parse_page, text
from .health import validate_status

COLLECTIONS = ("introductions", "lineup", "previews", "specials", "movies")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "page.schema.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate_upcoming_source(document: dict, payload: bytes) -> None:
    """A rolling announcements page can shrink; verify coverage of today's HTML."""
    soup = BeautifulSoup(payload.decode("utf-8"), "html.parser")
    body = soup.select_one('.post-body[itemprop="articleBody"]') or soup.select_one('.post-body')
    article = body.find_parent('article') if body is not None else None
    footer = article.select_one('.post-footer') if article is not None else None
    if body is None or footer is None or body in footer.parents or body.find_next(class_='post-footer') is not footer:
        raise ValueError(f"incomplete upcoming article: {document['source']['pageId']}")
    cards = body.select('.future_div')
    tables = body.find_all('table')
    titles = body.find_all(lambda node: class_has(node, 'future_title'))
    parsed = document['previews']
    if (not cards or len(cards) != len(tables) or len(cards) != len(titles) or len(cards) != len(parsed)
            or any('future_table' not in table.get('class', []) for table in tables)
            or [text(title) for title in titles] != [entry['title']['text'] for entry in parsed]):
        raise ValueError(
            f"upcoming source coverage mismatch: {document['source']['pageId']} "
            f"(cards={len(cards)}, tables={len(tables)}, titles={len(titles)}, parsed={len(parsed)})")


def validate_document(document: dict, payload: bytes, previous: dict | None = None) -> None:
    Draft202012Validator(read_json(SCHEMA_PATH)).validate(document)
    if document["source"]["htmlSha256"] != sha(payload):
        raise ValueError(f"raw HTML checksum mismatch: {document['source']['pageId']}")
    if not document["source"]["transport"]["tlsVerified"]:
        raise ValueError("unverified TLS snapshot cannot be published")
    upcoming = document['source']['pageType'] == 'upcoming'
    if upcoming:
        validate_upcoming_source(document, payload)
    ids = [entry["recordId"] for name in COLLECTIONS for entry in document[name]]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate source record IDs: {document['source']['pageId']}")
    for name in COLLECTIONS:
        for entry in document[name]:
            title = entry["titles"]["chinese"] if name == "introductions" else entry["title"]
            if not title["text"]:
                raise ValueError(f"missing title in {entry['recordId']}")
        if (previous and not (upcoming and name == 'previews')
                and len(previous[name]) >= 10 and len(document[name]) < len(previous[name]) * 0.7):
            raise ValueError(f"suspicious {name} count drop: {document['source']['pageId']} (review parser/source before publishing)")
    if document["source"]["pageType"] == "quarter" and not document["introductions"]:
        raise ValueError(f"quarter has no introductions: {document['source']['pageId']}")
    if document["source"]["pageType"] == "upcoming" and not document["previews"]:
        raise ValueError("upcoming page has no preview cards")
    if document["source"]["pageType"] == "special" and not document["specials"]:
        raise ValueError("special page has no cards")
    if document["source"]["pageType"] == "movie" and not document["movies"]:
        raise ValueError("movie page has no cards")


def validate_archive(root: Path) -> dict:
    manifest = read_json(root / "manifest.json")
    if manifest["schemaVersion"] != SCHEMA_VERSION or manifest["parserVersion"] != PARSER_VERSION:
        raise ValueError("manifest schema/parser version mismatch; run sync")
    pending = set(manifest.get('pendingUrls', []))
    retained = set(manifest.get('retainedUrls', []))
    if len(pending) != len(manifest.get('pendingUrls', [])) or len(retained) != len(manifest.get('retainedUrls', [])):
        raise ValueError('duplicate pending/retained URL')
    if (pending or retained) and manifest.get('statusPath') != 'status.json':
        raise ValueError('pending/retained pages require explicit health status')
    urls, ids = set(), set()
    documents = {}
    outgoing_urls = set()
    totals = {name: 0 for name in COLLECTIONS}
    for record in manifest["pages"]:
        page_path = root / record["jsonPath"]
        raw_path = root / record["rawHtmlPath"]
        if not page_path.resolve().is_relative_to(root.resolve()) or not raw_path.resolve().is_relative_to(root.resolve()):
            raise ValueError("archive path escapes data directory")
        document = read_json(page_path)
        payload = raw_path.read_bytes()
        validate_document(document, payload)
        source = document["source"]
        if source["url"] != record["url"] or source["pageId"] != record["pageId"] or source["rawHtmlPath"] != record["rawHtmlPath"]:
            raise ValueError("manifest and page source disagree")
        if source["htmlSha256"] != record["htmlSha256"]:
            raise ValueError("manifest checksum disagrees with page")
        if source["url"] in urls or source["pageId"] in ids:
            raise ValueError("duplicate page in manifest")
        urls.add(source["url"]); ids.add(source["pageId"])
        documents[source['url']] = document
        outgoing_urls.update(linked_articles(payload, source["url"]))
        # Reparse offline: schema-valid but stale or hand-edited JSON is rejected.
        if parse_page(payload, source) != document:
            raise ValueError(f"JSON is not reproducible from its raw snapshot: {source['pageId']}")
        counts = {name: len(document[name]) for name in COLLECTIONS}
        if counts != record["counts"]:
            raise ValueError("manifest record counts disagree with document")
        for name in COLLECTIONS:
            totals[name] += counts[name]
    discovered = set(manifest['discoveredUrls'])
    if (len(discovered) != len(manifest['discoveredUrls']) or pending != discovered - urls
            or retained != urls - discovered or totals != manifest["totals"]):
        raise ValueError("manifest totals/discovery mismatch")
    expected_paths = {record["jsonPath"] for record in manifest["pages"]}
    actual_paths = {str(p.relative_to(root)) for p in (root / "pages").glob("*.json")}
    if actual_paths != expected_paths:
        raise ValueError("unindexed or missing JSON pages")
    saved_discovery = {}
    for evidence in manifest["discovery"]:
        evidence_path = root / evidence["path"]
        if not evidence_path.resolve().is_relative_to(root.resolve()):
            raise ValueError("discovery evidence path escapes data directory")
        payload = evidence_path.read_bytes()
        if sha(payload) != evidence["sha256"]:
            raise ValueError("discovery evidence checksum mismatch")
        if evidence["url"] in saved_discovery:
            raise ValueError("duplicate discovery URL")
        saved_discovery[evidence["url"]] = (payload, evidence)

    class ReplayFetcher:
        def get(self, url):
            if url not in saved_discovery:
                raise ValueError(f"missing saved discovery page: {url}")
            return saved_discovery[url]

    # Reconstruct the discovery graph, rather than trusting mutually consistent
    # edited manifest counts. Replay cannot perform network requests.
    from .discovery import discover as replay_discovery
    seeds, _ = replay_discovery(ReplayFetcher())
    if set(seeds) | outgoing_urls != discovered:
        raise ValueError("archive does not cover all saved discovery/article links")
    state = 'untracked'
    if manifest.get('statusPath') is not None:
        if manifest['statusPath'] != 'status.json':
            raise ValueError('unsupported archive status path')
        status = read_json(root / 'status.json')
        validate_status(status, documents, pending)
        state = status['state']
    return {"state": state, "pages": len(ids), "pendingPages": len(pending), "retainedPages": len(retained), **totals}
