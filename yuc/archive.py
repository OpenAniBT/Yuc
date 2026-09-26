"""Validated, reproducible repository snapshots with all-or-nothing fetches."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

from . import PARSER_VERSION, SCHEMA_VERSION
from .discovery import BASE, discover, linked_articles, page_id, page_kind
from .fetch import Fetcher
from .parser import class_has, parse_page, text

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
    if len(manifest["pages"]) != len(manifest["discoveredUrls"]):
        raise ValueError("manifest discovery coverage mismatch")
    urls, ids = set(), set()
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
        outgoing_urls.update(linked_articles(payload, source["url"]))
        # Reparse offline: schema-valid but stale or hand-edited JSON is rejected.
        if parse_page(payload, source) != document:
            raise ValueError(f"JSON is not reproducible from its raw snapshot: {source['pageId']}")
        counts = {name: len(document[name]) for name in COLLECTIONS}
        if counts != record["counts"]:
            raise ValueError("manifest record counts disagree with document")
        for name in COLLECTIONS:
            totals[name] += counts[name]
    if urls != set(manifest["discoveredUrls"]) or totals != manifest["totals"]:
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
    if set(seeds) | outgoing_urls != urls:
        raise ValueError("archive does not cover all saved discovery/article links")
    return {"pages": len(ids), **totals}


def sync(root: Path, *, full: bool = False, fetcher: Fetcher | None = None) -> dict:
    fetcher = fetcher or Fetcher()
    existing = read_json(root / "manifest.json") if (root / "manifest.json").exists() else None
    if existing:
        # Refuse to build on corrupted inputs. Parser upgrades reparse cached bytes.
        for record in existing["pages"]:
            if sha((root / record["rawHtmlPath"]).read_bytes()) != record["htmlSha256"]:
                raise ValueError(f"existing raw snapshot corrupt: {record['pageId']}")
    robots_bytes, robots_info = fetcher.get(BASE + "robots.txt", allow_missing=True)
    robots = RobotFileParser()
    robots.parse(robots_bytes.decode("utf-8").splitlines() if robots_info["httpStatus"] == 200 else [])
    if not robots.can_fetch("AniBT-YucArchive", BASE) or not robots.can_fetch("AniBT-YucArchive", BASE + "archives/"):
        raise ValueError("robots.txt disallows archive discovery")
    urls, discovery_evidence = discover(fetcher)
    discovery_evidence["robots.txt"] = (robots_bytes, robots_info)
    staged: dict[str, bytes] = {}
    pages = []
    queue = list(urls)
    done = set()
    page_ids = set()
    network_changed = 0
    source_changes = []
    while queue:
        url = queue.pop(0)
        if url in done:
            continue
        if len(done) >= 1000:
            raise ValueError("article discovery exceeded 1000 pages")
        if not robots.can_fetch("AniBT-YucArchive", url):
            raise ValueError(f"robots.txt disallows discovered article: {url}")
        slug = page_id(url)
        if slug in page_ids:
            raise ValueError(f"colliding page slug: {slug}")
        page_ids.add(slug); done.add(url)
        path = f"pages/{slug}.json"
        raw_path = f"raw/{slug}.html"
        old = read_json(root / path) if (root / path).exists() else None
        validators = old["source"] if old and not full else None
        payload, fetched = fetcher.get(url, validators=validators)
        if fetched["httpStatus"] == 304:
            if not old:
                raise ValueError("unexpected 304 without a stored page")
            payload = (root / raw_path).read_bytes()
        if old and old["source"]["htmlSha256"] == sha(payload):
            source = old["source"]
        else:
            network_changed += 1
            source = {**fetched, "url": url, "pageId": slug, "pageType": page_kind(slug),
                      "quarterCode": slug if page_kind(slug) == "quarter" else None, "rawHtmlPath": raw_path}
        document = parse_page(payload, source)
        validate_document(document, payload, old)
        if (old and source['pageType'] == 'upcoming'
                and len(document['previews']) < len(old['previews'])):
            source_changes.append({'pageId': slug, 'collection': 'previews',
                                   'previousCount': len(old['previews']), 'currentCount': len(document['previews'])})
        staged[path] = json_bytes(document)
        staged[raw_path] = payload
        pages.append({"pageId": slug, "url": url, "pageType": source["pageType"], "title": document["title"],
                      "jsonPath": path, "rawHtmlPath": raw_path, "htmlSha256": sha(payload),
                      "counts": {name: len(document[name]) for name in COLLECTIONS}, "warnings": document["quality"]["warnings"]})
        for target in sorted(linked_articles(payload, url)):
            if target not in done and target not in queue:
                queue.append(target)
        print(f"parsed {slug}: " + ", ".join(f"{name}={len(document[name])}" for name in COLLECTIONS), flush=True)
    if existing and not set(existing["discoveredUrls"]).issubset(done):
        raise ValueError("previously archived pages disappeared from discovery; refusing automatic deletion")
    previous_discovery = {r["path"]: r for r in existing["discovery"]} if existing else {}
    evidence_records = []
    for name, (payload, fetched) in sorted(discovery_evidence.items()):
        path = "raw/discovery/" + name
        staged[path] = payload
        old = previous_discovery.get(path)
        if old and old["sha256"] == sha(payload):
            evidence_records.append(old)
        else:
            evidence_records.append({"path": path, "sha256": sha(payload), **fetched})
    pages.sort(key=lambda record: record["pageId"])
    manifest = {
        "schemaVersion": SCHEMA_VERSION, "parserVersion": PARSER_VERSION,
        "source": BASE, "attribution": {"author": "長門有C", "site": "長門番堂 / Yuc's Anime List",
                                         "license": "CC BY-NC-SA 4.0", "licenseUrl": "https://creativecommons.org/licenses/by-nc-sa/4.0/"},
        "discoveryMethod": "all archive pagination pages, homepage post links, article body and adjacent-post links",
        "discoveredUrls": sorted(done), "discovery": evidence_records,
        "pages": pages, "totals": {name: sum(p["counts"][name] for p in pages) for name in COLLECTIONS},
    }
    staged["manifest.json"] = json_bytes(manifest)
    # Fetch and parse everything before touching published data. Validate a complete
    # candidate directory, then replace only files whose bytes changed.
    with tempfile.TemporaryDirectory(prefix="yuc-candidate-") as tmp:
        candidate = Path(tmp)
        for path, payload in staged.items():
            target = candidate / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(payload)
        summary = validate_archive(candidate)
        changed_files = []
        for path, payload in staged.items():
            target = root / path
            if target.exists() and target.read_bytes() == payload:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_bytes(payload)
            os.replace(temporary, target)
            changed_files.append(path)
    report = {"completedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "full": full, "fetchedChangedPages": network_changed, "changedFiles": changed_files,
              "sourceChanges": source_changes, **summary}
    return report
