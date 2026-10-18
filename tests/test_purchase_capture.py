import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from asnb.network import NetworkTrace, purchase_response_summary


class PurchaseCaptureTests(unittest.TestCase):
    def test_business_classification_does_not_store_messages_or_tokens(self):
        for code, expected in [('068', 'INSUFFICIENT'), ('1001', 'BLOCKED')]:
            result = purchase_response_summary({'data': {'REJECTCODE': code,
                'REJECTREASON': 'private account details', 'token': 'secret'}})
            self.assertEqual(result['outcome'], expected)
            self.assertNotIn('private', json.dumps(result))
            self.assertNotIn('secret', json.dumps(result))
        result = purchase_response_summary({'TRANSACTIONSTATUS': 'SUCCESS', 'paymentURL': 'private'})
        self.assertEqual(result['statuses'], ['SUCCESS'])
        self.assertEqual(result['outcome'], 'UNKNOWN')  # Never declare payment completed.

    def test_capture_retains_only_redacted_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            trace = NetworkTrace(path)
            driver = Mock()
            driver.execute_cdp_cmd.return_value = {'body': json.dumps({'REJECTCODE': '068', 'token': 'secret'})}
            trace.consume('Network.requestWillBeSent', {'requestId': '1', 'timestamp': 1,
                'type': 'XHR', 'request': {'url': 'https://myasnb-api-v4.myasnb.com.my/v2/subscription/provisional/privateaccount/ASM',
                'method': 'POST', 'postData': '{"amount":5000,"fpxBankId":"privatebank","token":"secret"}'}})
            trace.capture_purchase(driver, '1')
            trace.close()
            record = json.loads(path.read_text())
            self.assertEqual(record['request_fields'], ['amount', 'fpxBankId'])
            self.assertEqual(record['outcome'], 'INSUFFICIENT')
            self.assertNotIn('privateaccount', path.read_text())
            self.assertNotIn('privatebank', path.read_text())
            self.assertNotIn('secret', path.read_text())
            private_path = Path(str(path) + '.purchase-private.jsonl')
            private = json.loads(private_path.read_text())
            self.assertEqual(json.loads(private['response_body'])['token'], 'secret')
            self.assertEqual(json.loads(private['request_body'])['fpxBankId'], 'privatebank')
            self.assertEqual(private_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('headers', private)

    def test_payment_handoff_details_retained_only_in_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            trace = NetworkTrace(path)
            trace.pending['1'] = {'purchase': True, 'endpoint': 'hashed', 'status': 200,
                                  'purchase_payload': '{}'}
            driver = Mock()
            body = {'paymentURL': 'https://example.test/pay?token=private-token',
                    'TRANSACTIONSTATUS': 'SUCCESS', 'form': {'transaction': 'private-reference'}}
            driver.execute_cdp_cmd.return_value = {'body': json.dumps(body)}
            trace.capture_purchase(driver, '1')
            trace.close()
            private = json.loads(Path(str(path) + '.purchase-private.jsonl').read_text())
            self.assertEqual(json.loads(private['response_body']), body)
            self.assertNotIn('private-token', path.read_text())
            self.assertNotIn('private-reference', path.read_text())

    def test_unrelated_requests_never_read_body(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = NetworkTrace(Path(directory) / 'trace.jsonl')
            driver = Mock()
            trace.capture_purchase(driver, 'unrelated')
            driver.execute_cdp_cmd.assert_not_called()
            trace.close()
