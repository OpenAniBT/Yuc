import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yuc.archive import sync, validate_archive
from yuc.discovery import article_url, discover
from yuc.fetch import FetchError, allowed_url
from test_parser import CARD, page, source


class FakeFetcher:
    def __init__(self, fail=False, modified=False):
        self.fail = fail
        self.modified = modified
        self.calls = []

    def get(self, url, *, allow_missing=False, validators=None):
        self.calls.append((url, bool(validators)))
        if url.endswith('/robots.txt'):
            return b'not found', {**source(), 'url': url, 'httpStatus': 404}
        if self.fail:
            raise FetchError('simulated interrupted download')
        payload = page(CARD + ('<p>updated</p>' if self.modified else ''))
        status = 304 if validators and not self.modified else 200
        return (b'' if status == 304 else payload), {**source(), 'httpStatus': status, 'etag': 'fixture'}


def fake_discovery(fetcher):
    urls = ['https://yuc.wiki/202610/']
    return urls, discovery_evidence(urls)


def discovery_evidence(urls):
    entries = ''.join(f'<div class="post-title"><a href="{url}">article</a></div>' for url in urls)
    archive = f'<div class="posts-collapse">目前共计 {len(urls)} 篇日志{entries}</div>'.encode()
    return {
        'homepage.html': (b'<html></html>', {**source(), 'url': 'https://yuc.wiki/'}),
        'archives.html': (archive, {**source(), 'url': 'https://yuc.wiki/archives/'}),
    }


def snapshot(root, *, include_status=False):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and (include_status or p.name != 'status.json')}


class ArchiveTests(unittest.TestCase):
    @patch('yuc.archive.discover', fake_discovery)
    def test_publish_validate_noop_and_304(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            first = sync(root, fetcher=FakeFetcher(), checked_at='2026-09-26T00:00:00+00:00')
            self.assertEqual(first['pages'], 1)
            self.assertEqual(first['introductions'], 1)
            before = snapshot(root)
            fetcher = FakeFetcher()
            second = sync(root, fetcher=fetcher, checked_at='2026-09-26T06:00:00+00:00')
            self.assertEqual(second['changedFiles'], ['status.json'])
            self.assertIn(('https://yuc.wiki/202610/', True), fetcher.calls)
            self.assertEqual(before, snapshot(root))
            self.assertEqual(validate_archive(root)['introductions'], 1)
            full_fetcher = FakeFetcher()
            self.assertEqual(sync(root, full=True, fetcher=full_fetcher,
                                  checked_at='2026-09-26T12:00:00+00:00')['changedFiles'], ['status.json'])
            self.assertIn(('https://yuc.wiki/202610/', False), full_fetcher.calls)
            third = sync(root, full=True, fetcher=FakeFetcher(modified=True))
            self.assertEqual(third['fetchedChangedPages'], 1)
            self.assertIn('updated', json.loads((root/'pages/202610.json').read_text())['content']['text'])

    @patch('yuc.archive.discover', fake_discovery)
    def test_failed_fetch_does_not_replace_previous_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            sync(root, fetcher=FakeFetcher())
            before = snapshot(root)
            result = sync(root, fetcher=FakeFetcher(fail=True))
            self.assertEqual(result['state'], 'degraded')
            self.assertTrue(result['published'])
            self.assertEqual(result['pageStates'][0]['state'], 'stale')
            self.assertEqual(before, snapshot(root))

    @patch('yuc.archive.discover', fake_discovery)
    def test_historical_quarter_count_drop_is_still_rejected(self):
        class QuarterFetcher(FakeFetcher):
            def __init__(self, count):
                super().__init__()
                self.count = count

            def get(self, url, **kwargs):
                payload, info = super().get(url, **kwargs)
                return (page(CARD * self.count) if info['httpStatus'] == 200 else payload), info

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            sync(root, fetcher=QuarterFetcher(10))
            before = snapshot(root)
            result = sync(root, full=True, fetcher=QuarterFetcher(6))
            self.assertEqual(result['state'], 'degraded')
            self.assertIn('suspicious introductions count drop', result['failures'][0]['message'])
            self.assertEqual(before, snapshot(root))

    def test_later_page_failure_preserves_snapshot_and_publishes_earlier_updates(self):
        class LaterFailure(FakeFetcher):
            def get(self, url, **kwargs):
                if url.endswith('/202610/'):
                    raise FetchError('last page failed after first page changed')
                return super().get(url, **kwargs)
        urls = ['https://yuc.wiki/202607/', 'https://yuc.wiki/202610/']
        with tempfile.TemporaryDirectory() as tmp, patch('yuc.archive.discover', return_value=(urls, discovery_evidence(urls))):
            root = Path(tmp) / 'data'
            sync(root, fetcher=FakeFetcher())
            before = snapshot(root)
            result = sync(root, full=True, fetcher=LaterFailure(modified=True))
            after = snapshot(root)
            self.assertEqual(result['state'], 'degraded')
            self.assertEqual(before['pages/202610.json'], after['pages/202610.json'])
            self.assertEqual(before['raw/202610.html'], after['raw/202610.html'])
            self.assertNotEqual(before['pages/202607.json'], after['pages/202607.json'])
            self.assertNotEqual(before['raw/202607.html'], after['raw/202607.html'])
            self.assertEqual(validate_archive(root)['pages'], 2)

    @patch('yuc.archive.discover', fake_discovery)
    def test_recursively_discovered_article_survives_next_sync(self):
        class Linked(FakeFetcher):
            def get(self, url, **kwargs):
                payload, info = super().get(url, **kwargs)
                if url.endswith('/202610/') and info['httpStatus'] == 200:
                    payload = page(CARD + '<a href="/extra/">extra article</a>')
                return payload, info
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            self.assertEqual(sync(root, fetcher=Linked())['pages'], 2)
            second = sync(root, fetcher=Linked())
            self.assertEqual(second['pages'], 2)
            self.assertLessEqual(set(second['changedFiles']), {'status.json'})

    def test_missing_article_rejected_even_when_manifest_totals_adjusted(self):
        urls = ['https://yuc.wiki/202607/', 'https://yuc.wiki/202610/']
        with tempfile.TemporaryDirectory() as tmp, patch('yuc.archive.discover', return_value=(urls, discovery_evidence(urls))):
            root = Path(tmp) / 'data'
            sync(root, fetcher=FakeFetcher())
            manifest = json.loads((root / 'manifest.json').read_text())
            manifest['pages'] = manifest['pages'][1:]
            manifest['discoveredUrls'] = manifest['discoveredUrls'][1:]
            manifest['totals']['introductions'] -= 1
            (root / 'manifest.json').write_text(json.dumps(manifest))
            (root / 'pages/202607.json').unlink()
            (root / 'raw/202607.html').unlink()
            with self.assertRaises(ValueError):
                validate_archive(root)

    @patch('yuc.archive.discover', fake_discovery)
    def test_tampered_json_and_raw_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            sync(root, fetcher=FakeFetcher())
            target = root/'pages/202610.json'
            original = target.read_bytes()
            document = json.loads(original)
            document['introductions'][0]['cast']['lines'] = []
            target.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'not reproducible'):
                validate_archive(root)
            target.write_bytes(original)
            (root/'raw/202610.html').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                validate_archive(root)

    @patch('yuc.archive.discover', fake_discovery)
    def test_discovery_cannot_silently_delete_existing_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            sync(root, fetcher=FakeFetcher())
            before = snapshot(root)
            with patch('yuc.archive.discover', side_effect=ValueError('no articles discovered')):
                result = sync(root, fetcher=FakeFetcher())
            self.assertEqual(result['state'], 'degraded')
            self.assertEqual(result['discovery']['state'], 'failed')
            self.assertEqual(before, snapshot(root))


class DiscoveryTests(unittest.TestCase):
    def test_normalize_and_bound_urls(self):
        for path in ['/202610', '/202610/', '/202610/index.html', 'http://yuc.wiki/202610/']:
            self.assertEqual(article_url(path), 'https://yuc.wiki/202610/')
        for path in ['/page/2/', '/archives/', '/tags/title/', '/image.png', '链接', '#A01', 'https://external.example/a/']:
            self.assertIsNone(article_url(path))
        for url in ['http://yuc.wiki/', 'https://evil.example/', 'https://yuc.wiki:444/', 'https://user@yuc.wiki/', 'https://yuc.wiki/?x=1']:
            self.assertFalse(allowed_url(url))
        self.assertTrue(allowed_url('https://yuc.wiki/202610/'))

    def test_archive_follows_pagination_and_checks_count(self):
        class Fetcher:
            def get(self, url):
                if url == 'https://yuc.wiki/':
                    return b'<html></html>', {}
                slug = '202610' if url.endswith('/archives/') else '202607'
                data = f'''<div class="posts-collapse">目前共计 2 篇日志<div class="post-title"><a href="/{slug}">title</a></div></div>
<div class="pagination"><a href="/archives/page/2/">2</a></div>'''
                return data.encode(), {}
        urls, evidence = discover(Fetcher())
        self.assertEqual(len(urls), 2)
        self.assertEqual(len(evidence), 3)
        class Incomplete(Fetcher):
            def get(self, url):
                data, info = super().get(url)
                return data.replace(b'2 ', b'3 '), info
        with self.assertRaisesRegex(ValueError, 'incomplete archive'):
            discover(Incomplete())


if __name__ == '__main__':
    unittest.main()
