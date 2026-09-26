import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yuc.archive import sync, validate_archive
from test_archive import discovery_evidence, snapshot
from test_parser import page, source


def upcoming_page(count):
    cards = ''.join(
        f'<div style="float:left"><div class="future_div">'
        f'<p class="future_type_c">小说</p><p class="future_date">2027</p>'
        f'<img data-src="https://images.example/{i}.jpg"></div>'
        f'<div><table class="future_table"><tr class="future_tr">'
        f'<td class="future_title_">节目 {i}</td></tr></table></div></div>'
        for i in range(count)
    )
    return page(cards, '新番卫星观测站').replace(
        b'</article>', b'<footer class="post-footer">article navigation</footer></article>')


class UpcomingFetcher:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kwargs):
        info = {**source('new', 'upcoming'), 'url': url}
        if url.endswith('/robots.txt'):
            return b'not found', {**info, 'httpStatus': 404}
        return self.payload, info


class UpcomingSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'data'
        urls = ['https://yuc.wiki/new/']
        discovery = patch('yuc.archive.discover', return_value=(urls, discovery_evidence(urls)))
        discovery.start()
        self.addCleanup(discovery.stop)
        sync(self.root, fetcher=UpcomingFetcher(upcoming_page(10)), checked_at='2026-09-26T00:00:00+00:00')

    def snapshot(self):
        return snapshot(self.root)

    def assert_rejected_refresh(self, payload, message):
        before = self.snapshot()
        result = sync(self.root, fetcher=UpcomingFetcher(payload))
        self.assertEqual(result['state'], 'degraded')
        self.assertTrue(result['published'])
        self.assertEqual(result['pageStates'][0]['state'], 'stale')
        self.assertIn(message, result['failures'][0]['message'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(validate_archive(self.root)['previews'], 10)

    def test_source_inventory_can_shrink_more_than_thirty_percent(self):
        # Same source layout, four announced titles removed by the publisher.
        result = sync(self.root, fetcher=UpcomingFetcher(upcoming_page(6)), checked_at='2026-09-26T06:00:00+00:00')
        self.assertEqual(result['previews'], 6)
        self.assertEqual(validate_archive(self.root)['previews'], 6)
        self.assertEqual(result['sourceChanges'], [{
            'pageId': 'new', 'collection': 'previews', 'previousCount': 10, 'currentCount': 6,
        }])
        published = json.loads((self.root / 'pages/new.json').read_text())
        self.assertEqual([p['title']['text'] for p in published['previews']], [f'节目 {i}' for i in range(6)])
        again = sync(self.root, fetcher=UpcomingFetcher(upcoming_page(6)), checked_at='2026-09-26T12:00:00+00:00')
        self.assertEqual(again['changedFiles'], ['status.json'])
        self.assertEqual(again['sourceChanges'], [])

    def test_missing_card_wrapper_is_rejected_even_below_count_threshold(self):
        payload = upcoming_page(10).replace(b'class="future_div"', b'class="changed_layout"', 1)
        self.assert_rejected_refresh(payload, 'upcoming source coverage mismatch')

    def test_truncated_page_is_rejected_and_previous_data_survives(self):
        payload = upcoming_page(6).split(b'<footer')[0]
        self.assert_rejected_refresh(payload, 'incomplete upcoming article')

    def test_empty_upcoming_page_is_still_rejected(self):
        self.assert_rejected_refresh(upcoming_page(0), 'missing article body')


if __name__ == '__main__':
    unittest.main()
