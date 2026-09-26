"""Exercise publication and offline recovery using synthetic upstream responses."""

import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from yuc.archive import sync, validate_archive
from yuc.fetch import FetchError
from test_archive import discovery_evidence, snapshot
from test_parser import CARD, page, source


BASE = 'https://yuc.wiki/'
OLDER = BASE + '202607/'
NEWER = BASE + '202610/'
EXTRA = BASE + 'extra/'


def checked_at(run):
    return (dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc)
            + dt.timedelta(hours=6 * run)).isoformat(timespec='seconds')


class SourceFetcher:
    """Replace the network boundary while retaining real discovery and parsing."""

    def __init__(self, urls=(OLDER, NEWER), *, responses=None, conditional=False, run=0):
        self.urls = urls
        self.responses = responses or {}
        self.conditional = conditional
        self.run = run
        self.calls = []

    def get(self, url, *, allow_missing=False, validators=None):
        self.calls.append((url, bool(validators)))
        info = {**source(), 'url': url, 'retrievedAt': checked_at(self.run), 'etag': 'fixture'}
        if url in self.responses:
            response = self.responses[url]
            if isinstance(response, Exception):
                raise response
            if isinstance(response, tuple):
                return response[0], {**info, **response[1]}
            return response, info
        if url == BASE + 'robots.txt':
            return b'not found', {**info, 'httpStatus': 404}
        evidence = discovery_evidence(self.urls)
        if url == BASE:
            return evidence['homepage.html'][0], info
        if url == BASE + 'archives/':
            return evidence['archives.html'][0], info
        if self.conditional and validators:
            return b'', {**info, 'httpStatus': 304}
        return page(CARD), info


class ResilientSyncTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'data'

    def run_sync(self, run=0, **kwargs):
        return sync(self.root, fetcher=SourceFetcher(run=run, **kwargs), checked_at=checked_at(run))

    def read(self, name):
        return json.loads((self.root / name).read_text())

    def page_states(self):
        return {entry['url']: entry for entry in self.read('status.json')['pages']}

    def assert_consistent_status(self, report):
        manifest = self.read('manifest.json')
        self.assertEqual(manifest['statusPath'], 'status.json')
        status = self.read(manifest['statusPath'])
        self.assertEqual(status['schemaVersion'], 1)
        self.assertEqual(status['state'], report['state'])
        self.assertEqual(status['checkedAt'], report['completedAt'])
        self.assertEqual(status['consecutiveFailures'], report['consecutiveFailures'])
        self.assertEqual(status['discovery'], report['discovery'])
        self.assertEqual(status['pages'], report['pageStates'])
        archived = {entry['url']: entry for entry in manifest['pages']}
        discovered = set(manifest['discoveredUrls'])
        self.assertEqual(set(manifest['pendingUrls']), discovered - archived.keys())
        self.assertEqual(set(manifest['retainedUrls']), archived.keys() - discovered)
        states = status['pages']
        self.assertEqual([entry['url'] for entry in states], sorted(archived.keys() | discovered))
        for entry in states:
            if entry['url'] in archived:
                record = archived[entry['url']]
                actual = hashlib.sha256((self.root / record['rawHtmlPath']).read_bytes()).hexdigest()
                self.assertEqual(entry['snapshotSha256'], actual)
                self.assertEqual(entry['pageId'], record['pageId'])
            else:
                self.assertEqual(entry['state'], 'unavailable')
                self.assertIsNone(entry['snapshotSha256'])
                self.assertIsNone(entry['lastSuccessAt'])
        self.assertEqual(validate_archive(self.root)['state'], status['state'])

    def test_one_failed_page_preserves_both_files_and_publishes_other_page(self):
        self.run_sync()
        before = snapshot(self.root)
        result = self.run_sync(1, responses={
            OLDER: page(CARD + '<p>new upstream text</p>'),
            NEWER: FetchError('HTTP 503 from source'),
        })
        after = snapshot(self.root)
        self.assertTrue(result['published'])
        self.assertEqual(result['state'], 'degraded')
        for name in ('pages/202610.json', 'raw/202610.html'):
            self.assertEqual(after[name], before[name])
            self.assertNotIn(name, result['changedFiles'])
        for name in ('pages/202607.json', 'raw/202607.html'):
            self.assertNotEqual(after[name], before[name])
            self.assertIn(name, result['changedFiles'])
        states = self.page_states()
        self.assertEqual(states[OLDER]['state'], 'current')
        self.assertEqual(states[NEWER]['state'], 'stale')
        self.assertEqual(states[NEWER]['lastSuccessAt'], checked_at(0))
        self.assertEqual(states[NEWER]['lastAttemptAt'], checked_at(1))
        self.assertEqual(result['failures'][0]['url'], NEWER)
        self.assertEqual(result['failures'][0]['pageId'], '202610')
        self.assert_consistent_status(result)

    def test_consecutive_page_failures_survive_runs_and_recovery_resets_them(self):
        self.run_sync()
        for run in (1, 2, 3):
            result = self.run_sync(run, responses={NEWER: FetchError('source unavailable')})
            failed = self.page_states()[NEWER]
            self.assertEqual(result['consecutiveFailures'], run)
            self.assertEqual(failed['consecutiveFailures'], run)
            self.assertEqual(failed['lastSuccessAt'], checked_at(0))
            self.assertEqual(failed['lastAttemptAt'], checked_at(run))
            self.assertTrue(failed['error']['kind'])
            self.assertIn('source unavailable', failed['error']['message'])
        result = self.run_sync(4, conditional=True)
        self.assertEqual(result['state'], 'healthy')
        self.assertEqual(result['consecutiveFailures'], 0)
        self.assertEqual(result['failures'], [])
        recovered = self.page_states()[NEWER]
        self.assertEqual(recovered['state'], 'current')
        self.assertEqual(recovered['consecutiveFailures'], 0)
        self.assertIsNone(recovered['error'])
        self.assertEqual(recovered['lastSuccessAt'], checked_at(4))
        self.assert_consistent_status(result)

    def test_new_missing_page_is_pending_then_becomes_a_valid_snapshot(self):
        self.run_sync()
        result = self.run_sync(1, urls=(OLDER, NEWER, EXTRA),
                               responses={EXTRA: FetchError('HTTP 404')})
        manifest = self.read('manifest.json')
        self.assertEqual(result['state'], 'degraded')
        self.assertEqual(result['pages'], 2)
        self.assertEqual(manifest['pendingUrls'], [EXTRA])
        self.assertFalse((self.root / 'pages/extra.json').exists())
        self.assertFalse((self.root / 'raw/extra.html').exists())
        missing = self.page_states()[EXTRA]
        self.assertEqual(missing['state'], 'unavailable')
        self.assertEqual(missing['consecutiveFailures'], 1)
        self.assert_consistent_status(result)
        result = self.run_sync(2, urls=(OLDER, NEWER, EXTRA))
        self.assertEqual(result['state'], 'healthy')
        self.assertEqual(result['pages'], 3)
        self.assertEqual(self.read('manifest.json')['pendingUrls'], [])
        recovered = self.page_states()[EXTRA]
        self.assertEqual(recovered['consecutiveFailures'], 0)
        self.assertEqual(recovered['lastSuccessAt'], checked_at(2))
        self.assertIsNone(recovered['error'])
        self.assert_consistent_status(result)

    def test_failed_linked_article_remains_in_replayed_discovery_graph(self):
        self.run_sync()
        result = self.run_sync(1, responses={
            OLDER: page(CARD + '<a href="/extra/">related source</a>'),
            EXTRA: FetchError('article temporarily absent'),
        })
        self.assertEqual(self.read('manifest.json')['pendingUrls'], [EXTRA])
        self.assert_consistent_status(result)
        result = self.run_sync(2, conditional=True)
        self.assertEqual(result['pages'], 3)
        self.assertIn(EXTRA, self.read('manifest.json')['discoveredUrls'])
        self.assert_consistent_status(result)

    def test_links_from_rejected_page_do_not_expand_archive_discovery(self):
        self.run_sync()
        fetcher = SourceFetcher(run=1, responses={
            OLDER: page('<a href="/extra/">invalid quarter without cards</a>'),
        })
        result = sync(self.root, fetcher=fetcher, checked_at=checked_at(1))
        self.assertEqual(result['state'], 'degraded')
        self.assertNotIn(EXTRA, [url for url, _ in fetcher.calls])
        self.assertEqual(self.read('manifest.json')['pendingUrls'], [])
        self.assertNotIn(EXTRA, self.read('manifest.json')['discoveredUrls'])
        self.assert_consistent_status(result)

    def test_discovery_network_count_and_layout_failures_reuse_saved_evidence(self):
        for failure in (FetchError('archive network failure'),
                        '<div class="posts-collapse">目前共计 3 篇日志</div>'.encode(),
                        b'<html>new archive layout</html>'):
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'data'
                sync(root, fetcher=SourceFetcher(), checked_at=checked_at(0))
                before = snapshot(root)
                fetcher = SourceFetcher(run=1, responses={
                    BASE + 'archives/': failure,
                    OLDER: page(CARD + '<p>independent article update</p>'),
                })
                result = sync(root, fetcher=fetcher, checked_at=checked_at(1))
                after = snapshot(root)
                self.assertEqual(result['state'], 'degraded')
                self.assertEqual(result['discovery']['state'], 'failed')
                self.assertEqual(result['discovery']['lastSuccessAt'], checked_at(0))
                for name in before:
                    if name.startswith('raw/discovery/'):
                        self.assertEqual(after[name], before[name])
                self.assertNotEqual(after['raw/202607.html'], before['raw/202607.html'])
                self.assertIn(OLDER, [url for url, _ in fetcher.calls])
                self.assertIn(NEWER, [url for url, _ in fetcher.calls])
                self.assertEqual(validate_archive(root)['pages'], 2)

    def test_discovery_failure_counter_and_success_reset_are_persisted(self):
        self.run_sync()
        for run in (1, 2, 3):
            result = self.run_sync(run, responses={BASE: FetchError('homepage unavailable')})
            self.assertEqual(result['discovery']['consecutiveFailures'], run)
            self.assertEqual(result['discovery']['lastAttemptAt'], checked_at(run))
            self.assertEqual(result['consecutiveFailures'], run)
            self.assertTrue(all(p['state'] == 'current' for p in result['pageStates']))
        result = self.run_sync(4)
        self.assertEqual(result['discovery']['state'], 'current')
        self.assertEqual(result['discovery']['consecutiveFailures'], 0)
        self.assertEqual(result['discovery']['lastSuccessAt'], checked_at(4))
        self.assertIsNone(result['discovery']['error'])
        self.assert_consistent_status(result)

    def test_robots_failure_or_disallow_makes_no_further_requests(self):
        self.run_sync()
        before = snapshot(self.root)
        for run, failure in enumerate((FetchError('robots network failure'),
                                       b'User-agent: *\nDisallow: /\n'), start=1):
            with self.subTest(failure=str(failure)):
                fetcher = SourceFetcher(run=run, responses={BASE + 'robots.txt': failure})
                result = sync(self.root, fetcher=fetcher, checked_at=checked_at(run))
                self.assertEqual(fetcher.calls, [(BASE + 'robots.txt', False)])
                self.assertEqual(result['state'], 'degraded')
                self.assertEqual(result['discovery']['state'], 'failed')
                self.assertTrue(all(p['state'] == 'stale' for p in result['pageStates']))
                self.assertEqual(snapshot(self.root), before)
                self.assert_consistent_status(result)

    def test_disappeared_seed_is_retained_and_explicit_in_manifest(self):
        self.run_sync()
        original = (self.root / 'raw/202607.html').read_bytes()
        result = self.run_sync(1, urls=(NEWER,))
        manifest = self.read('manifest.json')
        self.assertEqual(manifest['discoveredUrls'], [NEWER])
        self.assertEqual(manifest['retainedUrls'], [OLDER])
        self.assertEqual(manifest['pendingUrls'], [])
        self.assertEqual(result['pages'], 2)
        self.assertEqual((self.root / 'raw/202607.html').read_bytes(), original)
        self.assert_consistent_status(result)

    def test_identical_content_only_advances_health_heartbeat_for_200_and_304(self):
        self.run_sync()
        before = snapshot(self.root)
        retrieved_at = self.read('pages/202610.json')['source']['retrievedAt']
        for run, conditional in ((1, False), (2, True)):
            result = self.run_sync(run, conditional=conditional)
            self.assertEqual(result['changedFiles'], ['status.json'])
            self.assertEqual(snapshot(self.root), before)
            self.assertEqual(self.read('pages/202610.json')['source']['retrievedAt'], retrieved_at)
            self.assertEqual(self.read('status.json')['checkedAt'], checked_at(run))
            for entry in self.page_states().values():
                self.assertEqual(entry['lastAttemptAt'], checked_at(run))
                self.assertEqual(entry['lastSuccessAt'], checked_at(run))
            self.assert_consistent_status(result)

    def test_corrupt_existing_archive_is_fatal_before_fetch_and_never_written(self):
        self.run_sync()
        for name in ('raw/202610.html', 'pages/202610.json', 'raw/discovery/archives.html'):
            with self.subTest(path=name):
                target = self.root / name
                original = target.read_bytes()
                target.write_bytes(b'corrupted existing snapshot')
                before = snapshot(self.root, include_status=True)
                fetcher = SourceFetcher(run=1)
                try:
                    with self.assertRaises((ValueError, json.JSONDecodeError)):
                        sync(self.root, fetcher=fetcher, checked_at=checked_at(1))
                    self.assertEqual(snapshot(self.root, include_status=True), before)
                    self.assertEqual(fetcher.calls, [])
                finally:
                    target.write_bytes(original)

    def test_tampered_manifest_cannot_hide_a_missing_page_as_healthy(self):
        self.run_sync()
        manifest = self.read('manifest.json')
        removed = manifest['pages'].pop(0)
        manifest['discoveredUrls'].remove(removed['url'])
        manifest['totals']['introductions'] -= 1
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        (self.root / removed['jsonPath']).unlink()
        (self.root / removed['rawHtmlPath']).unlink()
        status = self.read('status.json')
        status['pages'] = [entry for entry in status['pages'] if entry['url'] != removed['url']]
        (self.root / 'status.json').write_text(json.dumps(status))
        with self.assertRaises(ValueError):
            validate_archive(self.root)

    def test_pending_page_cannot_be_relabelled_current_or_removed_from_status(self):
        self.run_sync(0, urls=(OLDER, NEWER, EXTRA), responses={EXTRA: FetchError('HTTP 404')})
        original = (self.root / 'status.json').read_bytes()
        for mode in ('current', 'missing'):
            with self.subTest(mode=mode):
                status = json.loads(original)
                status['state'] = 'healthy'
                status['consecutiveFailures'] = 0
                if mode == 'current':
                    entry = next(p for p in status['pages'] if p['url'] == EXTRA)
                    entry.update(state='current', consecutiveFailures=0, error=None,
                                 lastSuccessAt=checked_at(0))
                else:
                    status['pages'] = [p for p in status['pages'] if p['url'] != EXTRA]
                (self.root / 'status.json').write_text(json.dumps(status))
                with self.assertRaises(ValueError):
                    validate_archive(self.root)

    def test_colliding_candidate_page_paths_are_fatal_without_any_writes(self):
        self.run_sync()
        before = snapshot(self.root, include_status=True)
        with self.assertRaisesRegex(ValueError, 'colli'):
            self.run_sync(1, urls=(OLDER, NEWER, BASE + 'a/b/', BASE + 'a__b/'),
                          responses={OLDER: page(CARD + '<p>must not publish</p>')})
        self.assertEqual(snapshot(self.root, include_status=True), before)

    def test_no_safe_bootstrap_does_not_publish_an_empty_archive(self):
        with self.assertRaises((FetchError, ValueError)):
            self.run_sync(responses={OLDER: FetchError('unavailable'), NEWER: FetchError('unavailable')})
        self.assertEqual(snapshot(self.root, include_status=True), {})

    def test_legacy_manifest_initializes_last_success_from_saved_source(self):
        self.run_sync()
        manifest = self.read('manifest.json')
        for name in ('statusPath', 'pendingUrls', 'retainedUrls'):
            manifest.pop(name)
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        (self.root / 'status.json').unlink()
        old_success = self.read('pages/202610.json')['source']['retrievedAt']
        self.assertEqual(validate_archive(self.root)['pages'], 2)
        result = self.run_sync(1, responses={NEWER: FetchError('failed during migration')})
        self.assertEqual(self.page_states()[NEWER]['lastSuccessAt'], old_success)
        self.assertEqual(self.page_states()[NEWER]['consecutiveFailures'], 1)
        self.assert_consistent_status(result)


if __name__ == '__main__':
    unittest.main()
