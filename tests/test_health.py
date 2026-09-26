import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_archive import FakeFetcher, fake_discovery
from yuc.archive import sync
from yuc.health import check_status


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'

    @patch('yuc.archive.discover', fake_discovery)
    def test_health_freshness_explicitly_expires_without_another_run(self):
        sync(self.root, fetcher=FakeFetcher(), checked_at='2026-09-26T00:00:00+00:00')
        fresh = check_status(self.root, now=dt.datetime(2026, 9, 26, 23, tzinfo=dt.timezone.utc))
        self.assertEqual(fresh['state'], 'healthy')
        self.assertTrue(fresh['fresh'])
        stale = check_status(self.root, now=dt.datetime(2026, 9, 27, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(stale['state'], 'stale')
        self.assertFalse(stale['fresh'])
        self.assertEqual(stale['failures'][0]['kind'], 'freshness')

    @patch('yuc.archive.discover', fake_discovery)
    def test_failed_refresh_is_not_healthy_despite_recent_check(self):
        sync(self.root, fetcher=FakeFetcher(), checked_at='2026-09-26T00:00:00+00:00')
        sync(self.root, fetcher=FakeFetcher(fail=True), checked_at='2026-09-26T06:00:00+00:00')
        result = check_status(self.root, now=dt.datetime(2026, 9, 26, 7, tzinfo=dt.timezone.utc))
        self.assertEqual(result['state'], 'degraded')
        self.assertTrue(result['fresh'])
        self.assertEqual(result['pageStates'][0]['lastSuccessAt'], '2026-09-26T00:00:00+00:00')
        self.assertEqual(result['consecutiveFailures'], 1)

    @patch('yuc.archive.discover', fake_discovery)
    def test_aggregate_health_cannot_hide_a_stale_page(self):
        sync(self.root, fetcher=FakeFetcher(), checked_at='2026-09-26T00:00:00+00:00')
        sync(self.root, fetcher=FakeFetcher(fail=True), checked_at='2026-09-26T06:00:00+00:00')
        path = self.root / 'status.json'
        status = json.loads(path.read_text())
        status['state'] = 'healthy'
        status['consecutiveFailures'] = 0
        path.write_text(json.dumps(status))
        with self.assertRaisesRegex(ValueError, 'aggregate archive health'):
            check_status(self.root)

    def test_missing_state_does_not_report_healthy(self):
        result = check_status(self.root)
        self.assertEqual(result['state'], 'missing')
        self.assertFalse(result['fresh'])


if __name__ == '__main__':
    unittest.main()
