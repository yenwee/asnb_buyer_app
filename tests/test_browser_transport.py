import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from asnb.api import APIReadError
from asnb.browser_transport import subscription_post, sync_cookies
from asnb.http_portal import HTTPPortal
from asnb.handoff import HandoffError
from tests.test_handoff import fixture


class BrowserTransportTests(unittest.TestCase):
    def test_subscription_uses_only_chrome_and_parses_068(self):
        with closing(HTTPPortal()) as client:
            client.account, client.authenticated = 'DEMO', True
            client.driver = Mock()
            client.driver.execute_async_script.return_value = {
                'status':200, 'headers':{'content-type':'application/json'}, 'headersMs':123,
                'text':'{"data":{"TRANSACTIONSTATUS":"Reject","REJECTCODE":"068"}}'}
            client.session.post = Mock()
            _, context = fixture()
            with patch('asnb.browser_transport.sync_cookies'):
                self.assertEqual(client.purchase(context), 'INSUFFICIENT')
            client.session.post.assert_not_called()
            client.driver.execute_async_script.assert_called_once()
            self.assertEqual(client.last_write['transport'], 'chrome-fetch')
            self.assertEqual(client.last_write['response_headers_ms'], 123)
            self.assertFalse(client.submission_locked)

    def test_chrome_uncertain_response_never_falls_back_to_python_or_retries(self):
        client = HTTPPortal()
        self.addCleanup(client.close)
        client.account, client.authenticated, client.driver = 'DEMO', True, Mock()
        client.driver.execute_async_script.return_value = {'error':'uncertain'}
        client.session.post = Mock()
        _, context = fixture()
        for _ in range(2):
            with self.assertRaises(HandoffError):
                client.purchase(context)
        client.session.post.assert_not_called()
        client.driver.execute_async_script.assert_called_once()
        self.assertTrue(client.submission_locked)

    def test_cookie_copy_failure_does_not_discard_accepted_order(self):
        import json
        response, context = fixture()
        client = HTTPPortal()
        self.addCleanup(client.close)
        client.account, client.authenticated, client.driver = 'DEMO', True, Mock()
        client.driver.execute_async_script.return_value = {
            'status':200, 'headers':{}, 'headersMs':100, 'text':json.dumps(response)}
        with patch('asnb.browser_transport.sync_cookies',side_effect=APIReadError()):
            self.assertEqual(client.purchase(context), 'SUCCESS')
        self.assertTrue(client.submission_locked)
        self.assertEqual(client.last_write['cookie_sync'], 'unavailable')

    def test_cookie_rotation_updates_http_session_without_foreign_domains(self):
        client = HTTPPortal()
        self.addCleanup(client.close)
        client.session.cookies.set('TokenID','old',domain='.myasnb.com.my',path='/')
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {'cookies':[
            {'name':'TokenID','value':'new','domain':'.myasnb.com.my','path':'/','secure':True},
            {'name':'foreign','value':'private','domain':'evil.invalid'}]}
        sync_cookies(driver, client)
        self.assertEqual(client.session.cookies.get('TokenID'), 'new')
        self.assertIsNone(client.session.cookies.get('foreign'))

    def test_driver_failure_has_static_message_and_one_dispatch(self):
        driver = Mock()
        driver.execute_async_script.side_effect = RuntimeError('private account URL')
        with self.assertRaisesRegex(APIReadError, '^Chrome subscription outcome unavailable; no transport retry$'):
            subscription_post(driver, 'DEMO', 'AS1M', {}, 60)
        driver.execute_async_script.assert_called_once()
        driver.set_script_timeout.assert_called_once_with(70)
