import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from asnb.network import NetworkTrace


class NetworkTests(unittest.TestCase):
    def test_intentional_asset_blocks_are_separate_from_network_failures(self):
        with tempfile.TemporaryDirectory() as root:
            trace = NetworkTrace(Path(root, 'trace.jsonl'))
            try:
                trace.consume('Network.loadingFailed', {'requestId': 'banner',
                    'timestamp': 1, 'blockedReason': 'inspector'})
                self.assertEqual(trace.counts['blocked_assets'], 1)
                self.assertEqual(trace.counts['failed_requests'], 0)
            finally:
                trace.close()

    def test_secrets_never_persist_and_sizes_are_counted(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'trace.jsonl')
            trace = NetworkTrace(path)
            try:
                trace.consume('Network.requestWillBeSent', {
                    'requestId': 'private-id', 'timestamp': 1, 'type': 'Fetch',
                    'request': {'url': 'https://www.myasnb.com.my/api/customer-secret?token=secret-query',
                                'method': 'POST', 'headers': {'Authorization': 'secret-auth'},
                                'postData': 'secret-password'},
                })
                trace.consume('Network.responseReceived', {
                    'requestId': 'private-id', 'response': {'status': 429,
                    'headers': {'Set-Cookie': 'secret-cookie', 'Retry-After': '120'}},
                })
                trace.consume('Network.loadingFinished', {
                    'requestId': 'private-id', 'timestamp': 1.25, 'encodedDataLength': 123,
                })
                trace.outcome('submission', 'BLOCKED')
                content = path.read_text()
                for secret in ('secret', 'private-id', 'myasnb.com.my', 'Authorization', 'Set-Cookie'):
                    self.assertNotIn(secret, content)
                record = json.loads(content.splitlines()[0])
                self.assertEqual(record['duration_ms'], 250)
                self.assertEqual(record['retry_after_seconds'], 120)
                self.assertEqual(trace.summary()['received_bytes'], 123)
            finally:
                trace.close()

    def test_buffers_and_disk_rotation_are_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            trace = NetworkTrace(Path(root, 'trace.jsonl'))
            try:
                for index in range(600):
                    trace.consume('Network.requestWillBeSent', {'requestId': str(index),
                        'timestamp': 1, 'type': 'Fetch',
                        'request': {'url': 'https://www.myasnb.com.my/api/test', 'method': 'GET'}})
                self.assertEqual(len(trace.pending), 512)
                self.assertEqual(trace.counts['evicted_requests'], 88)
                trace.handler.maxBytes = 200
                for _ in range(100):
                    trace.outcome('submission', 'SUCCESS')
                self.assertLessEqual(len(list(Path(root).iterdir())), 4)
            finally:
                trace.close()

    def test_third_party_urls_and_bad_retry_headers_not_saved(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'trace.jsonl')
            trace = NetworkTrace(path)
            try:
                trace.consume('Network.requestWillBeSent', {'requestId': 'x', 'timestamp': 0,
                    'type': 'Fetch', 'request': {'url': 'https://tracker.example/secret', 'method': 'GET'}})
                trace.consume('Network.loadingFinished', {'requestId': 'x', 'timestamp': 1,
                                                         'encodedDataLength': 45})
                self.assertEqual(path.read_text(), '')
                self.assertEqual(trace.summary()['received_bytes'], 45)
                self.assertEqual(trace.summary()['requests'], 1)
            finally:
                trace.close()

    def test_collection_failure_is_nonfatal_without_logging_exception_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'trace.jsonl')
            trace = NetworkTrace(path)
            driver = Mock()
            driver.get_log.side_effect = RuntimeError('secret')
            try:
                trace.drain(driver, force=True)
                self.assertEqual(trace.summary()['collection_errors'], 1)
                self.assertNotIn('secret', path.read_text())
            finally:
                trace.close()
