"""Durable per-page freshness and consecutive failure state."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

FRESHNESS_HOURS = 24


def timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('health timestamps must include a timezone')
    return parsed.astimezone(dt.timezone.utc)


def outcome(previous: dict | None, checked_at: str, error: dict | None, *, last_success: str | None = None) -> dict:
    previous = previous or {}
    return {
        'lastAttemptAt': checked_at,
        'lastSuccessAt': checked_at if error is None else previous.get('lastSuccessAt', last_success),
        'consecutiveFailures': previous.get('consecutiveFailures', 0) + 1 if error else 0,
        'error': error,
    }


def discovery_outcome(previous: dict | None, checked_at: str, error: dict | None) -> dict:
    return {'state': 'failed' if error else 'current', **outcome(previous, checked_at, error)}


def page_outcome(url: str, page_id: str | None, document: dict | None, previous: dict | None,
                 checked_at: str, error: dict | None) -> dict:
    source = document['source'] if document else {}
    return {
        'url': url, 'pageId': page_id,
        'state': ('stale' if document else 'unavailable') if error else 'current',
        **outcome(previous, checked_at, error, last_success=source.get('retrievedAt')),
        'snapshotSha256': source.get('htmlSha256'),
    }


def build_status(checked_at: str, discovery: dict, pages: list[dict], previous: dict | None) -> dict:
    degraded = discovery['state'] != 'current' or any(p['state'] != 'current' for p in pages)
    return {
        'schemaVersion': 1, 'checkedAt': checked_at, 'state': 'degraded' if degraded else 'healthy',
        'consecutiveFailures': (previous or {}).get('consecutiveFailures', 0) + 1 if degraded else 0,
        'discovery': discovery, 'pages': sorted(pages, key=lambda p: p['url']),
    }


def failures(status: dict) -> list[dict]:
    result = []
    discovery = status['discovery']
    if discovery['error']:
        result.append({'url': None, 'pageId': None, **discovery['error']})
    for page in status['pages']:
        if page['error']:
            result.append({'url': page['url'], 'pageId': page['pageId'], **page['error']})
    return result


def validate_status(status: dict, documents: dict[str, dict], pending: set[str]) -> None:
    if status.get('schemaVersion') != 1 or status.get('state') not in {'healthy', 'degraded'}:
        raise ValueError('invalid archive status schema/state')
    checked = timestamp(status['checkedAt'])
    records = status['pages']
    urls = [p['url'] for p in records]
    if len(urls) != len(set(urls)) or set(urls) != set(documents) | pending:
        raise ValueError('health page coverage mismatch')
    for record in [status['discovery'], *records]:
        count = record['consecutiveFailures']
        error = record['error']
        if type(count) is not int or count < 0:
            raise ValueError('invalid consecutive failure count')
        if timestamp(record['lastAttemptAt']) != checked:
            raise ValueError('status contains a page not checked in this run')
        last_success = record['lastSuccessAt']
        if last_success is not None and timestamp(last_success) > checked:
            raise ValueError('last success is later than current check')
        if record['state'] == 'current':
            if count != 0 or error is not None or last_success is None or timestamp(last_success) != checked:
                raise ValueError('current state has stale/failed health evidence')
        elif count < 1 or not isinstance(error, dict) or not error.get('kind') or not error.get('message'):
            raise ValueError('failed state requires error and consecutive failure count')
    if status['discovery']['state'] not in {'current', 'failed'}:
        raise ValueError('invalid discovery state')
    for record in records:
        url = record['url']
        if url in documents:
            source = documents[url]['source']
            if (record['state'] not in {'current', 'stale'} or record['snapshotSha256'] != source['htmlSha256']
                    or record['pageId'] != source['pageId']):
                raise ValueError('health and archived snapshot disagree')
        elif (record['state'] != 'unavailable' or record['snapshotSha256'] is not None
              or record['lastSuccessAt'] is not None):
            raise ValueError('pending page must be unavailable without a snapshot')
    degraded = status['discovery']['state'] == 'failed' or any(p['state'] != 'current' for p in records)
    if status['state'] != ('degraded' if degraded else 'healthy'):
        raise ValueError('aggregate archive health disagrees with page health')
    count = status['consecutiveFailures']
    if type(count) is not int or (count < 1 if degraded else count != 0):
        raise ValueError('aggregate failure count disagrees with archive health')


def check_status(root: Path, *, now: dt.datetime | None = None) -> dict:
    path = root / 'status.json'
    if not path.is_file():
        return {'state': 'missing', 'fresh': False, 'checkedAt': None, 'consecutiveFailures': 0,
                'failures': [{'url': None, 'pageId': None, 'kind': 'health', 'message': 'No successful maintenance run has written status.json'}],
                'pageStates': []}
    status = json.loads(path.read_text(encoding='utf-8'))
    # Use structural validation too; a forged healthy summary must not suppress a failure.
    from .validation import validate_archive
    validate_archive(root)
    now = now or dt.datetime.now(dt.timezone.utc)
    age = (now - timestamp(status['checkedAt'])).total_seconds()
    fresh = -300 <= age <= FRESHNESS_HOURS * 3600
    errors = failures(status)
    if not fresh:
        errors.append({'url': None, 'pageId': None, 'kind': 'freshness',
                       'message': f'Archive status is outside its {FRESHNESS_HOURS}-hour freshness window'})
    return {**{k: status[k] for k in ['checkedAt', 'consecutiveFailures', 'discovery']},
            'state': status['state'] if fresh else 'stale', 'fresh': fresh,
            'failures': errors, 'pageStates': status['pages']}
