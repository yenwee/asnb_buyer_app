import unittest
from unittest.mock import Mock, patch

from asnb.api import APIReadError, APISessionExpired
from asnb.api_runner import run_http_runner, cooldown_status, hold_status
from asnb.handoff import HandoffError
from asnb.http_portal import LoginBootstrapUnavailable
from asnb.driver import DriverSetupError
from selenium.common.exceptions import TimeoutException


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
        self.hooks.fund_cooldowns.remaining_cooldown.return_value = 300
        self.browser_factory = Mock()

    def run_runner(self, subscription_transport='chrome', loops=1, funds=None):
        funds = funds or ['ASM 3']
        if subscription_transport is None:
            return run_http_runner('demo','private',funds,'5000','Hong Leong Bank','',loops,6,{},self.browser_factory,self.hooks)
        return run_http_runner('demo','private',funds,'5000','Hong Leong Bank','',loops,6,{},self.browser_factory,self.hooks,
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

    def test_cooldown_snapshot_wakes_for_first_expiry_and_shows_each_fund(self):
        tracker = Mock()
        tracker.remaining_cooldown.side_effect = lambda fund: {'ASM':150,'ASM 2':12.2,'ASM 3':90}[fund]
        delay, message = cooldown_status(['ASM','ASM 2','ASM 3'], tracker)
        self.assertEqual(delay, 12.2)
        self.assertIn('ASM: 2:30 | ASM 2: 0:13 | ASM 3: 1:30', message)
        self.assertIn('Next eligible: ASM 2 in 0:13', message)

    def test_long_cooldowns_keep_status_updates_bounded_to_one_minute(self):
        tracker = Mock()
        tracker.remaining_cooldown.side_effect = lambda fund: {'ASM':180,'ASM 2':120}[fund]
        delay, message = cooldown_status(['ASM','ASM 2'], tracker)
        self.assertEqual(delay, 60)
        self.assertIn('Next eligible: ASM 2 in 2:00', message)

    def test_preflight_does_not_restart_another_funds_active_cooldown(self):
        self.hooks.fund_cooldowns.is_on_cooldown.side_effect = lambda fund: fund == 'ASM 2'
        self.client.preflight.return_value = {
            'ASM 3':{'state':'ELIGIBLE'}, 'ASM 2':{'state':'BLOCKED','seconds':300}}
        self.client.purchase.return_value = 'BLOCKED'
        self.client.block_seconds = 300
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'):
            self.run_runner()
        self.hooks.fund_cooldowns.mark_blocked.assert_called_once_with('ASM 3',300)

    def test_all_fund_hold_reasons_are_logged_before_any_purchase(self):
        self.client.preflight.return_value = {
            'ASM 3':{'state':'BLOCKED','reason':'FUND_BLOCKED','seconds':300},
            'ASM':{'state':'INACTIVE','reason':'FUND_INACTIVE','seconds':300},
            'ASM 2':{'state':'BANK_UNAVAILABLE','reason':'BANK_INACTIVE','seconds':60}}
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client):
            self.run_runner()
        messages = [call.args[0] for call in self.hooks.print.call_args_list if call.args]
        self.assertTrue(any('ASM 3: BLOCKED (portal reports fund blocked)' in message for message in messages))
        self.assertTrue(any('ASM: INACTIVE (fund inactive)' in message for message in messages))
        self.assertTrue(any('ASM 2: BANK_UNAVAILABLE (selected bank unavailable)' in message for message in messages))
        self.client.purchase.assert_not_called()
        self.browser_factory.assert_not_called()

    def test_business_hours_reason_is_static_and_does_not_log_raw_metadata(self):
        message = hold_status('ASM', {'state':'CHANNEL_CLOSED','reason':'OUTSIDE_BUSINESS_HOURS',
                                     'private':'private account'}, 60)
        self.assertIn('outside fund business hours', message)
        self.assertNotIn('private', message)

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
                self.run_runner('http',loops=2)
        self.assertEqual(events, ['create','INSUFFICIENT','close','SUCCESS','create','close'])
        copy.assert_called_once()
        self.assertEqual(self.client.purchase.call_count, 2)

    def test_http_repeated_068_does_not_reinitialize_closed_chrome(self):
        self.client.purchase.return_value = 'INSUFFICIENT'
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session') as transfer, \
                patch('asnb.api_runner.sync_cookies') as copy:
            self.run_runner('http',loops=30)
        self.assertEqual(self.client.purchase.call_count, 30)
        self.browser_factory.assert_called_once()
        self.hooks.close_webdriver.assert_called_once()
        transfer.assert_called_once()
        copy.assert_called_once()
        self.assertTrue(self.client.context.call_args_list[0].kwargs['reuse_preflight'])
        self.assertTrue(all(call.kwargs['reuse_preflight'] for call in self.client.context.call_args_list))
        self.assertEqual(self.client.preflight.call_count,30)

    def _configure_round_robin(self):
        funds = ['ASM','ASM 2','ASM 3']
        codes = {'ASM':'ASM','ASM 2':'ASW','ASM 3':'AS1M'}
        self.client.preflight.return_value = {fund:{'state':'ELIGIBLE'} for fund in funds}
        self.client.context.side_effect = lambda fund,*a,**kw: {'account':'DEMO','fund_id':codes[fund]}
        return funds

    def test_round_robin_attempts_each_fund_once_before_second_attempt(self):
        funds = self._configure_round_robin()
        order = []
        self.client.purchase.side_effect = lambda context,*a: order.append(context['fund_id']) or 'INSUFFICIENT'
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.sync_cookies'), \
                patch('asnb.api_runner.random.uniform',return_value=5):
            self.run_runner('http',loops=2,funds=funds)
        self.assertEqual(order,['ASM','ASW','AS1M','ASM','ASW','AS1M'])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list],[5]*6)
        self.browser_factory.assert_called_once()

    def test_round_robin_skips_blocked_fund_on_later_turns(self):
        funds = self._configure_round_robin()
        blocked, order = set(), []
        self.hooks.fund_cooldowns.is_on_cooldown.side_effect = lambda fund: fund in blocked
        self.hooks.fund_cooldowns.mark_blocked.side_effect = lambda fund,*a: blocked.add(fund)
        self.client.block_seconds = 300
        def outcome(context,*args):
            order.append(context['fund_id'])
            return 'BLOCKED' if context['fund_id']=='ASM' else 'INSUFFICIENT'
        self.client.purchase.side_effect = outcome
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.sync_cookies'):
            self.run_runner('http',loops=2,funds=funds)
        self.assertEqual(order,['ASM','ASW','AS1M','ASW','AS1M','ASW'])
        self.hooks.fund_cooldowns.mark_blocked.assert_called_once_with('ASM',300)

    def test_unknown_response_stops_round_robin_before_next_fund(self):
        funds = self._configure_round_robin()
        order = []
        def outcome(context,*args):
            order.append(context['fund_id'])
            if context['fund_id']=='ASW':
                self.client.submission_locked = True
                raise HandoffError('uncertain')
            return 'INSUFFICIENT'
        self.client.purchase.side_effect = outcome
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.sync_cookies'):
            with self.assertRaises(KeyboardInterrupt):
                self.run_runner('http',loops=2,funds=funds)
        self.assertEqual(order,['ASM','ASW'])
        self.hooks.wait_for_submission_review.assert_called_once()

    def test_acceptance_pauses_round_robin_before_next_fund(self):
        funds = self._configure_round_robin()
        def accepted(*args):
            self.client.submission_locked=True
            return 'SUCCESS'
        self.client.purchase.side_effect=accepted
        self.hooks.pause_for_payment.side_effect=KeyboardInterrupt
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.sync_cookies'), \
                patch('asnb.api_runner.open_confirmation'):
            with self.assertRaises(KeyboardInterrupt):
                self.run_runner('http',loops=2,funds=funds)
        self.client.purchase.assert_called_once()
        self.assertEqual(self.client.purchase.call_args.args[0]['fund_id'],'ASM')


    def test_rejected_attempts_reuse_one_chrome_transport(self):
        self.client.purchase.return_value = 'INSUFFICIENT'
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), patch('asnb.api_runner.time.sleep'), \
                patch('asnb.api_runner.import_http_session') as transfer:
            self.run_runner(loops=30)
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

    def test_resume_logout_failure_does_not_misclassify_accepted_purchase(self):
        def accepted(*args):
            self.client.submission_locked = True
            return 'SUCCESS'
        self.client.purchase.side_effect = accepted
        self.hooks.pause_for_payment.return_value = 'RESUME'
        self.client.logout.side_effect = [APIReadError('logout unavailable'), {'logoutHTTP':None}]
        with patch('asnb.api_runner.HTTPPortal', return_value=self.client), \
                patch('asnb.api_runner.import_http_session'), \
                patch('asnb.api_runner.open_confirmation'):
            self.run_runner()
        self.client.purchase.assert_called_once()
        self.assertEqual(self.client.login.call_count, 2)
        self.client.reset.assert_called_once()
        self.assertFalse(self.client.submission_locked)
        self.hooks.wait_for_submission_review.assert_not_called()

    def test_browser_setup_failure_prevents_purchase(self):
        self.client.purchase.return_value='SUCCESS'
        self.browser_factory.side_effect=RuntimeError('browser unavailable')
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client):
            with self.assertRaises(KeyboardInterrupt):self.run_runner()
        self.client.purchase.assert_not_called()
        self.browser_factory.assert_called_once()

    def test_browser_startup_recovers_without_relogin_or_early_purchase(self):
        self.browser_factory.side_effect = [DriverSetupError('private details'), Mock()]
        self.client.purchase.return_value = 'INSUFFICIENT'
        with patch('asnb.api_runner.HTTPPortal', return_value=self.client), \
                patch('asnb.api_runner.import_http_session'):
            self.run_runner(loops=2)
        self.client.login.assert_called_once()
        self.client.purchase.assert_called_once()
        self.assertEqual(self.browser_factory.call_count, 2)
        self.assertEqual(self.sleep.call_args_list[0].args, (5,))
        self.assertNotIn('private details', str(self.hooks.print.call_args_list))

    def test_portfolio_navigation_timeout_closes_browser_before_retry(self):
        self.client.purchase.return_value = 'INSUFFICIENT'
        with patch('asnb.api_runner.HTTPPortal', return_value=self.client), \
                patch('asnb.api_runner.import_http_session', side_effect=[TimeoutException('private URL'), None]):
            self.run_runner(loops=2)
        self.assertEqual(self.browser_factory.call_count, 2)
        self.client.purchase.assert_called_once()
        self.client.login.assert_called_once()
        self.assertEqual(self.hooks.close_webdriver.call_count, 2)
        self.assertNotIn('private URL', str(self.hooks.print.call_args_list))

    def test_repeated_chrome_failures_never_dispatch_and_backoff_is_capped(self):
        self.browser_factory.side_effect = DriverSetupError('unavailable')
        with patch('asnb.api_runner.HTTPPortal', return_value=self.client):
            self.run_runner(loops=7)
        self.client.purchase.assert_not_called()
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [5,10,20,40,60,60,60])
        self.client.login.assert_called_once()

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

    def test_transient_public_bootstrap_retries_with_capped_backoff_before_ordering(self):
        self.client.login.side_effect=[LoginBootstrapUnavailable('public GET unavailable')]*6+[True]
        self.client.purchase.return_value='BLOCKED'
        self.client.block_seconds=300
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'):
            self.run_runner()
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list],[5,10,20,40,60,60])
        self.assertEqual(self.client.login.call_count,7)
        self.client.purchase.assert_called_once()

    def test_bootstrap_throttle_waits_in_bounded_chunks_before_retry(self):
        self.client.login.side_effect=[LoginBootstrapUnavailable('public HTTP 429',retry_after=120),True]
        self.client.purchase.return_value='BLOCKED'
        self.client.block_seconds=300
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client), \
                patch('asnb.api_runner.import_http_session'):
            self.run_runner()
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list],[60,60])
        self.assertEqual(self.client.login.call_count,2)

    def test_expiry_then_bootstrap_failure_recovers_without_purchase_replay(self):
        self.client.login.side_effect=[True,LoginBootstrapUnavailable('public timeout'),True]
        self.client.preflight.side_effect=APISessionExpired()
        with patch('asnb.api_runner.HTTPPortal',return_value=self.client):
            self.run_runner()
        self.assertEqual(self.client.login.call_count,3)
        self.client.purchase.assert_not_called()
        self.browser_factory.assert_not_called()
        self.hooks.stats.record_refresh.assert_called_once()
