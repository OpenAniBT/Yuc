"""Refresh independent pages, preserving validated snapshots when sources fail."""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.robotparser import RobotFileParser

from . import PARSER_VERSION, SCHEMA_VERSION
from .discovery import BASE, discover, linked_articles, page_id, page_kind
from .fetch import Fetcher, FetchError
from .health import build_status, discovery_outcome, failures, page_outcome, timestamp
from .parser import parse_page
# Keep the existing public imports for callers; validation has one offline owner.
from .validation import COLLECTIONS, json_bytes, read_json, sha, validate_archive, validate_document

CRAWL_BUDGET_SECONDS = 600
REJECTED_BYTES_BUDGET = 16 * 1024 * 1024


@contextmanager
def writer_lock(root: Path):
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / f'.{root.name}.sync.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another synchronization owns this data directory') from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class CrawlBudget:
    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.deadline = time.monotonic() + CRAWL_BUDGET_SECONDS

    def get(self, url, **kwargs):
        if time.monotonic() >= self.deadline:
            raise FetchError('run crawl budget exhausted; preserving previous snapshot')
        return self.fetcher.get(url, **kwargs)


def source_error(kind: str, exc: Exception) -> dict:
    # Public source errors only; avoid unbounded HTML/tracebacks in durable state.
    return {'kind': kind, 'message': f'{type(exc).__name__}: {exc}'[:1200]}


class RejectedPages:
    def __init__(self, report_dir: Path | None):
        self.root = report_dir / 'rejected' if report_dir else None
        self.remaining = REJECTED_BYTES_BUDGET
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)
            owner = self.root / '.yuc-generated'
            if owner.is_file() and owner.read_text() == 'yuc-rejected-v1\n':
                for path in self.root.iterdir():
                    if re.fullmatch(r'[0-9a-f]{20}\.(?:html|json)', path.name) and path.is_file():
                        path.unlink()
            owner.write_text('yuc-rejected-v1\n')

    def save(self, url: str, payload: bytes | None, error: dict) -> None:
        if self.root is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        key = sha(url.encode())[:20]
        saved = payload is not None and len(payload) <= self.remaining
        if saved:
            (self.root / f'{key}.html').write_bytes(payload)
            self.remaining -= len(payload)
        (self.root / f'{key}.json').write_bytes(json_bytes({
            'url': url, 'error': error, 'htmlSaved': saved,
            'htmlSha256': sha(payload) if payload is not None else None,
        }))


def previous_discovery(root: Path, existing: dict) -> tuple[list[str], dict]:
    evidence = {Path(r['path']).name: ((root / r['path']).read_bytes(), r) for r in existing['discovery']}
    by_url = {record['url']: (payload, record) for payload, record in evidence.values()}

    class ReplayFetcher:
        def get(self, url):
            return by_url[url]

    from .discovery import discover as replay_discovery
    seeds, _ = replay_discovery(ReplayFetcher())
    return seeds, evidence


def manifest_record(document: dict) -> dict:
    source = document['source']
    return {
        'pageId': source['pageId'], 'url': source['url'], 'pageType': source['pageType'],
        'title': document['title'], 'jsonPath': f"pages/{source['pageId']}.json",
        'rawHtmlPath': source['rawHtmlPath'], 'htmlSha256': source['htmlSha256'],
        'counts': {name: len(document[name]) for name in COLLECTIONS},
        'warnings': document['quality']['warnings'],
    }


def sync(root: Path, *, full: bool = False, fetcher: Fetcher | None = None,
         report_dir: Path | None = None, checked_at: str | None = None) -> dict:
    with writer_lock(root):
        return _sync(root, full=full, fetcher=fetcher or Fetcher(), report_dir=report_dir,
                     checked_at=checked_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'))


def _sync(root: Path, *, full: bool, fetcher, report_dir: Path | None, checked_at: str) -> dict:
    timestamp(checked_at)
    existing = read_json(root / 'manifest.json') if (root / 'manifest.json').exists() else None
    if existing:
        # Failed refreshes can retain only snapshots proven valid before any I/O.
        validate_archive(root)
    old_status = read_json(root / 'status.json') if existing and existing.get('statusPath') else None
    old_states = {p['url']: p for p in old_status['pages']} if old_status else {}
    documents, payloads = {}, {}
    for record in existing['pages'] if existing else []:
        documents[record['url']] = read_json(root / record['jsonPath'])
        payloads[record['url']] = (root / record['rawHtmlPath']).read_bytes()
    fetcher = CrawlBudget(fetcher)
    rejected = RejectedPages(report_dir)
    discovery_error = crawl_error = None
    robots = RobotFileParser()
    try:
        robots_bytes, robots_info = fetcher.get(BASE + 'robots.txt', allow_missing=True)
        robots.parse(robots_bytes.decode('utf-8').splitlines() if robots_info['httpStatus'] == 200 else [])
        if not robots.can_fetch('AniBT-YucArchive', BASE) or not robots.can_fetch('AniBT-YucArchive', BASE + 'archives/'):
            raise ValueError('robots.txt disallows archive discovery')
    except Exception as exc:
        crawl_error = discovery_error = source_error('robots', exc)
    if discovery_error is None:
        try:
            seeds, evidence = discover(fetcher)
            evidence['robots.txt'] = (robots_bytes, robots_info)
        except Exception as exc:
            discovery_error = source_error('discovery', exc)
    if discovery_error:
        if not existing:
            raise ValueError(f"cannot bootstrap without verified discovery: {discovery_error['message']}")
        seeds, evidence = previous_discovery(root, existing)
    discovery_state = discovery_outcome(old_status['discovery'] if old_status else None, checked_at, discovery_error)
    queue = sorted(set(seeds) | set(documents) | set(existing.get('pendingUrls', []) if existing else []))
    done, identities, states = set(), {}, {}
    source_changes = []
    network_changed = 0
    while queue:
        url = queue.pop(0)
        if url in done:
            continue
        if len(done) >= 1000:
            raise ValueError('article discovery exceeded 1000 pages')
        done.add(url)
        old = documents.get(url)
        payload = None
        error = None
        slug = None
        try:
            slug = page_id(url)
        except ValueError as exc:
            error = source_error('identity', exc)
        if slug is not None:
            if slug in identities and identities[slug] != url:
                raise ValueError(f'colliding page slug: {slug}')
            identities[slug] = url
        if error is None and crawl_error:
            error = dict(crawl_error)
        if error is None and not robots.can_fetch('AniBT-YucArchive', url):
            error = source_error('robots', ValueError(f'robots.txt disallows article: {url}'))
        if error is None:
            phase = 'fetch'
            try:
                payload, fetched = fetcher.get(url, validators=old['source'] if old and not full else None)
                if fetched['httpStatus'] == 304:
                    if not old:
                        raise ValueError('unexpected 304 without a stored page')
                    payload = payloads[url]
                elif fetched['httpStatus'] != 200:
                    raise FetchError(f"unexpected article status: {fetched['httpStatus']}")
                if old and old['source']['htmlSha256'] == sha(payload):
                    source = old['source']
                else:
                    source = {**fetched, 'url': url, 'pageId': slug, 'pageType': page_kind(slug),
                              'quarterCode': slug if page_kind(slug) == 'quarter' else None,
                              'rawHtmlPath': f'raw/{slug}.html'}
                phase = 'parse'
                document = parse_page(payload, source)
                phase = 'validation'
                validate_document(document, payload, old)
                # Only validated bytes replace an earlier source snapshot.
                documents[url], payloads[url] = document, payload
                if not old or old['source']['htmlSha256'] != sha(payload):
                    network_changed += 1
                if (old and source['pageType'] == 'upcoming'
                        and len(document['previews']) < len(old['previews'])):
                    source_changes.append({'pageId': slug, 'collection': 'previews',
                                           'previousCount': len(old['previews']), 'currentCount': len(document['previews'])})
            except Exception as exc:
                error = source_error(phase, exc)
        states[url] = page_outcome(url, slug, documents.get(url), old_states.get(url), checked_at, error)
        if error:
            rejected.save(url, payload, error)
            print('retained ' + json.dumps({'page': slug or url, **error}, ensure_ascii=False), flush=True)
        else:
            print(f"checked {slug}: " + ', '.join(f'{name}={len(documents[url][name])}' for name in COLLECTIONS), flush=True)
        if url in payloads:
            for target in sorted(linked_articles(payloads[url], url)):
                if target not in done and target not in queue:
                    queue.append(target)
    if not documents:
        raise ValueError('cannot bootstrap without at least one validated page')
    discovered = set(seeds)
    for url, payload in payloads.items():
        discovered.update(linked_articles(payload, url))
    pending = discovered - set(documents)
    retained = set(documents) - discovered
    status = build_status(checked_at, discovery_state,
                          [states[url] for url in sorted(set(documents) | pending)], old_status)
    staged = {}
    pages = []
    for url, document in documents.items():
        record = manifest_record(document)
        pages.append(record)
        staged[record['jsonPath']] = json_bytes(document)
        staged[record['rawHtmlPath']] = payloads[url]
    prior_evidence = {r['path']: r for r in existing['discovery']} if existing else {}
    evidence_records = []
    for name, (payload, fetched) in sorted(evidence.items()):
        path = 'raw/discovery/' + name
        staged[path] = payload
        old = prior_evidence.get(path)
        evidence_records.append(old if old and old['sha256'] == sha(payload)
                                else {**fetched, 'path': path, 'sha256': sha(payload)})
    manifest = {
        'schemaVersion': SCHEMA_VERSION, 'parserVersion': PARSER_VERSION, 'source': BASE,
        'attribution': {'author': '長門有C', 'site': "長門番堂 / Yuc's Anime List",
                        'license': 'CC BY-NC-SA 4.0', 'licenseUrl': 'https://creativecommons.org/licenses/by-nc-sa/4.0/'},
        'discoveryMethod': 'all archive pagination pages, homepage post links, article body and adjacent-post links',
        'discoveredUrls': sorted(discovered), 'pendingUrls': sorted(pending), 'retainedUrls': sorted(retained),
        'discovery': evidence_records, 'statusPath': 'status.json',
        'pages': sorted(pages, key=lambda p: p['pageId']),
        'totals': {name: sum(p['counts'][name] for p in pages) for name in COLLECTIONS},
    }
    staged['manifest.json'] = json_bytes(manifest)
    staged['status.json'] = json_bytes(status)
    with tempfile.TemporaryDirectory(prefix='yuc-candidate-') as tmp:
        candidate = Path(tmp)
        for path, payload in staged.items():
            target = candidate / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        summary = validate_archive(candidate)
        changed_files = []
        for path, payload in staged.items():
            target = root / path
            if target.exists() and target.read_bytes() == payload:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.tmp')
            temporary.write_bytes(payload)
            os.replace(temporary, target)
            changed_files.append(path)
    return {
        'completedAt': checked_at, 'full': full, 'state': status['state'], 'published': True,
        'consecutiveFailures': status['consecutiveFailures'], 'discovery': discovery_state,
        'pageStates': status['pages'], 'failures': failures(status),
        'fetchedChangedPages': network_changed, 'changedFiles': changed_files,
        'sourceChanges': source_changes, **summary,
    }
