import unittest
from unittest.mock import Mock, patch

from asnb.api import APIReadError, APISessionExpired
from asnb.api_runner import run_http_runner
from asnb.handoff import HandoffError


class APIRunnerTests(unittest.TestCase):
    def setUp(self):
        readiness = patch('asnb.api_runner.wait_for_portal_ready')
        self.readiness = readiness.start()
        self.addCleanup(readiness.stop)
        sleeps = patch('asnb.api_runner.time.sleep')
        self.sleep = sleeps.start()
        self.addCleanup(sleeps.stop)
        self.client = Mock(authenticated=True,submission_locked=False,account='DEMO',confirmation={'demo':True})
        self.client.preflight.return_value = {'ASM 3':{'state':'ELIGIBLE'}}
        self.client.context.return_value = {'account':'DEMO','fund_id':'AS1M'}
        self.client.logout.return_value = {'logoutHTTP':200}
        self.client.balance_lines.return_value = ['Portfolio: 1 funds | current holdings value: RM 1,000.00']
        self.client.purchase_diagnostic.return_value = '{"http":200}'
        self.hooks = Mock()
        self.hooks.fund_cooldowns.is_on_cooldown.return_value = False
        self.browser_factory = Mock()

    def run_runner(self, subscription_transport='chrome'):
        if subscription_transport is None:
            return run_http_runner('demo','private',['ASM 3'],'5000','Hong Leong Bank','',1,6,{},self.browser_factory,self.hooks)
        return run_http_runner('demo','private',['ASM 3'],'5000','Hong Leong Bank','',1,6,{},self.browser_factory,self.hooks,
                               subscription_transport=subscription_transport)

    def test_runner_defaults_to_initialized_http(self):
        self.client.purchase.return_value = 'BLOCKED'
        self.client.block_seconds = 300
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.sync_cookies') as copy:
            self.run_runner(None)
        self.assertIsNone(self.client.driver)
        copy.assert_called_once()
        self.browser_factory.assert_called_once()
        self.hooks.close_webdriver.assert_called_once()

    def test_http_waits_and_copies_browser_cookies_before_purchase(self):
        events = []
        self.readiness.side_effect = lambda *a: events.append('ready')
        def rejected(*args):
            events.append('http-purchase')
            self.assertIsNone(self.client.driver)
            return 'BLOCKED'
        self.client.purchase.side_effect = rejected
        self.client.block_seconds = 300
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session') as transfer, \
                patch('asnb.api_runner.sync_cookies',side_effect=lambda *a:events.append('copy-cookies')) as copy:
            self.run_runner('http')
        self.assertEqual(events, ['ready','copy-cookies','http-purchase'])
        self.sleep.assert_not_called()
        self.readiness.assert_called_once()
        self.browser_factory.assert_called_once()
        transfer.assert_called_once()
        copy.assert_called_once()
        self.client.purchase.assert_called_once()

    def test_initialized_http_uncertain_response_never_retries_or_creates_second_browser(self):
        def uncertain(*args):
            self.client.submission_locked = True
            raise HandoffError('uncertain')
        self.client.purchase.side_effect = uncertain
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.sync_cookies') as copy:
            with self.assertRaises(KeyboardInterrupt):
                self.run_runner('http')
        self.client.purchase.assert_called_once()
        self.browser_factory.assert_called_once()
        copy.assert_called_once()

    def test_http_acceptance_reimports_rotated_cookies_before_confirmation(self):
        events = []
        def accepted(*args):
            self.assertIsNone(self.client.driver)
            self.client.submission_locked = True
            events.append('http-accepted')
            return 'SUCCESS'
        self.client.purchase.side_effect = accepted
        self.hooks.pause_for_payment.side_effect = KeyboardInterrupt
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session',side_effect=lambda *a:events.append('import-session')), \
                patch('asnb.api_runner.sync_cookies'), \
                patch('asnb.api_runner.open_confirmation',side_effect=lambda *a:events.append('confirmation')):
            with self.assertRaises(KeyboardInterrupt):
                self.run_runner('http')
        self.assertEqual(events, ['import-session','http-accepted','import-session','confirmation'])
        self.browser_factory.assert_called_once()
        self.client.purchase.assert_called_once()

    def test_http_closes_warmup_after_068_and_reopens_only_after_acceptance(self):
        events = []
        outcomes = iter(['INSUFFICIENT', 'SUCCESS'])
        def purchase(*args):
            outcome = next(outcomes)
            events.append(outcome)
            if outcome == 'SUCCESS':
                self.client.submission_locked = True
            return outcome
        self.client.purchase.side_effect = purchase
        self.browser_factory.side_effect = lambda: events.append('create') or Mock()
        self.hooks.close_webdriver.side_effect = lambda *a: events.append('close')
        self.hooks.pause_for_payment.side_effect = KeyboardInterrupt
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.sync_cookies') as copy, \
                patch('asnb.api_runner.open_confirmation'):
            with self.assertRaises(KeyboardInterrupt):
                self.run_runner('http')
        self.assertEqual(events, ['create','INSUFFICIENT','close','SUCCESS','create','close'])
        copy.assert_called_once()
        self.assertEqual(self.client.purchase.call_count, 2)

    def test_http_repeated_068_does_not_reinitialize_closed_chrome(self):
        self.client.purchase.return_value = 'INSUFFICIENT'
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session') as transfer, \
                patch('asnb.api_runner.sync_cookies') as copy:
            self.run_runner('http')
        self.assertEqual(self.client.purchase.call_count, 30)
        self.browser_factory.assert_called_once()
        self.hooks.close_webdriver.assert_called_once()
        transfer.assert_called_once()
        copy.assert_called_once()


    def test_rejected_attempts_reuse_one_chrome_transport(self):
        self.client.purchase.return_value = 'INSUFFICIENT'
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), patch('asnb.api_runner.time.sleep'), \
                patch('asnb.api_runner.import_http_session') as transfer:
            self.run_runner()
        self.assertEqual(self.client.purchase.call_count,30)
        self.browser_factory.assert_called_once()
        transfer.assert_called_once()
        self.assertEqual(self.client.driver, self.browser_factory.return_value)
        self.client.logout.assert_called_once()
        self.hooks.print.assert_any_call('Portfolio: 1 funds | current holdings value: RM 1,000.00')

    def test_success_then_browser_transfer_and_manual_payment_in_order(self):
        events=[]
        self.client.login.side_effect=lambda *a:events.append('http-login')
        def accepted(*a):
            events.append('purchase-success');self.client.submission_locked=True;return 'SUCCESS'
        self.client.purchase.side_effect=accepted
        self.browser_factory.side_effect=lambda:events.append('create-chrome') or Mock()
        self.hooks.pause_for_payment.side_effect=lambda *a:events.append('manual-payment') or 'RESUME'
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session',side_effect=lambda *a:events.append('transfer-session')), \
                patch('asnb.api_runner.open_confirmation',side_effect=lambda *a:events.append('agreement-page')):
            self.run_runner()
        self.assertEqual(events[:6],['http-login','create-chrome','transfer-session','purchase-success','agreement-page','manual-payment'])
        self.assertEqual(self.client.purchase.call_count,1)
        self.hooks.close_webdriver.assert_called_once()
        self.assertEqual(self.client.login.call_count,2)  # Explicit Resume starts a fresh HTTP session.

    def test_unknown_order_stops_without_second_transport_or_retry(self):
        def uncertain(*a):
            self.client.submission_locked=True;raise HandoffError('uncertain')
        self.client.purchase.side_effect=uncertain
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), patch('asnb.api_runner.import_http_session'):
            with self.assertRaises(KeyboardInterrupt):self.run_runner()
        self.client.purchase.assert_called_once()
        self.browser_factory.assert_called_once()
        self.hooks.wait_for_submission_review.assert_called_once()

    def test_browser_setup_failure_prevents_purchase(self):
        self.client.purchase.return_value='SUCCESS'
        self.browser_factory.side_effect=RuntimeError('browser unavailable')
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client):
            with self.assertRaises(KeyboardInterrupt):self.run_runner()
        self.client.purchase.assert_not_called()
        self.browser_factory.assert_called_once()

    def test_session_transfer_failure_prevents_purchase(self):
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session',side_effect=HandoffError('transfer failed')):
            with self.assertRaises(KeyboardInterrupt):
                self.run_runner()
        self.client.purchase.assert_not_called()
        self.browser_factory.assert_called_once()

    def test_portal_not_initialized_prevents_first_purchase(self):
        self.readiness.side_effect = APIReadError('not ready')
        self.client.preflight.side_effect = [{'ASM 3':{'state':'ELIGIBLE'}}]
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'):
            self.run_runner()
        self.client.purchase.assert_not_called()
        self.assertFalse(self.client.submission_locked)

    def test_accepted_confirmation_failure_never_issues_second_order(self):
        def accepted(*a):
            self.client.submission_locked = True
            return 'SUCCESS'
        self.client.purchase.side_effect = accepted
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.open_confirmation',side_effect=HandoffError('render failed')):
            with self.assertRaises(KeyboardInterrupt):
                self.run_runner()
        self.client.purchase.assert_called_once()
        self.browser_factory.assert_called_once()

    def test_unexpected_notification_failure_after_acceptance_cannot_restart_ordering(self):
        def accepted(*a):self.client.submission_locked=True;return 'SUCCESS'
        self.client.purchase.side_effect=accepted
        self.hooks.pause_for_payment.side_effect=RuntimeError('notification failed')
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'),patch('asnb.api_runner.open_confirmation'):
            with self.assertRaises(KeyboardInterrupt):self.run_runner()
        self.client.purchase.assert_called_once()

    def test_expired_read_session_refreshes_through_http_without_browser(self):
        self.client.preflight.side_effect=APISessionExpired()
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client):self.run_runner()
        self.assertEqual(self.client.login.call_count,2)
        self.client.purchase.assert_not_called()
        self.browser_factory.assert_not_called()

    def test_login_error_stops_without_credential_retry_or_browser(self):
        self.client.login.side_effect=APIReadError('review authentication')
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client):
            with self.assertRaises(KeyboardInterrupt):self.run_runner()
        self.client.login.assert_called_once()
        self.client.purchase.assert_not_called()
        self.browser_factory.assert_not_called()
