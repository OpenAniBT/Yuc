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


class ArchiveTests(unittest.TestCase):
    @patch('yuc.archive.discover', fake_discovery)
    def test_publish_validate_noop_and_304(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            first = sync(root, fetcher=FakeFetcher())
            self.assertEqual(first['pages'], 1)
            self.assertEqual(first['introductions'], 1)
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            fetcher = FakeFetcher()
            second = sync(root, fetcher=fetcher)
            self.assertEqual(second['changedFiles'], [])
            self.assertIn(('https://yuc.wiki/202610/', True), fetcher.calls)
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            self.assertEqual(validate_archive(root)['introductions'], 1)
            full_fetcher = FakeFetcher()
            self.assertEqual(sync(root, full=True, fetcher=full_fetcher)['changedFiles'], [])
            self.assertIn(('https://yuc.wiki/202610/', False), full_fetcher.calls)
            third = sync(root, full=True, fetcher=FakeFetcher(modified=True))
            self.assertEqual(third['fetchedChangedPages'], 1)
            self.assertIn('updated', json.loads((root/'pages/202610.json').read_text())['content']['text'])

    @patch('yuc.archive.discover', fake_discovery)
    def test_failed_fetch_does_not_replace_previous_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'data'
            sync(root, fetcher=FakeFetcher())
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with self.assertRaises(FetchError):
                sync(root, fetcher=FakeFetcher(fail=True))
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})

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
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with self.assertRaisesRegex(ValueError, 'suspicious introductions count drop'):
                sync(root, full=True, fetcher=QuarterFetcher(6))
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_later_page_failure_does_not_publish_earlier_updates(self):
        class LaterFailure(FakeFetcher):
            def get(self, url, **kwargs):
                if url.endswith('/202610/'):
                    raise FetchError('last page failed after first page changed')
                return super().get(url, **kwargs)
        urls = ['https://yuc.wiki/202607/', 'https://yuc.wiki/202610/']
        with tempfile.TemporaryDirectory() as tmp, patch('yuc.archive.discover', return_value=(urls, discovery_evidence(urls))):
            root = Path(tmp) / 'data'
            sync(root, fetcher=FakeFetcher())
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with self.assertRaises(FetchError):
                sync(root, full=True, fetcher=LaterFailure(modified=True))
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})

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
            self.assertEqual(second['changedFiles'], [])

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
            with self.assertRaisesRegex(ValueError, 'does not cover all saved discovery'):
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
            with patch('yuc.archive.discover', return_value=([], {})):
                with self.assertRaisesRegex(ValueError, 'refusing automatic deletion'):
                    sync(root, fetcher=FakeFetcher())


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
