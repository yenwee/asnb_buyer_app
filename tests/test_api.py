import json
import unittest
from unittest.mock import Mock, patch

from asnb.api import PortalAPI, APIReadError, APISessionExpired, APIThrottled
from asnb import main


class Response:
    def __init__(self, body=None, status=200, headers=None, content=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = content if content is not None else json.dumps(body or {}).encode()
        self.closed = False

    def iter_content(self, size):
        for offset in range(0, len(self.content), size):
            yield self.content[offset:offset + size]

    def close(self):
        self.closed = True


class APITests(unittest.TestCase):
    def setUp(self):
        self.driver = Mock()
        self.driver.execute_script.return_value = {
            'url': 'https://myasnb-api-v4.myasnb.com.my/v2/portfolio/owned', 'account': 'owned', 'ua': 'test-browser',
        }
        self.driver.execute_cdp_cmd.return_value = {'cookies': [
            {'name': 'session', 'value': 'private-cookie', 'domain': '.myasnb.com.my', 'path': '/', 'secure': True},
            {'name': 'unrelated', 'value': 'ignore', 'domain': 'example.com', 'path': '/'},
        ]}
        self.api = PortalAPI(self.driver)
        self.addCleanup(self.api.close)

    def test_owned_session_discovery_and_route_allowlist(self):
        self.api._sync()
        self.assertEqual(self.api.account, 'owned')
        self.assertEqual(len(self.api.session.cookies), 1)
        with patch.object(self.api.session, 'get') as get:
            for path in ['/v2/subscription/provisional/owned/ASM', '/v2/directdebit/set/owned/ASM',
                         'https://evil.example/', '/v2/portfolio/someone_else']:
                with self.subTest(path=path), self.assertRaises(APIReadError):
                    self.api._get(path)
            get.assert_not_called()

    def test_cannot_discover_foreign_origin_or_changed_account(self):
        self.driver.execute_script.return_value['url'] = 'https://evil.example/v2/portfolio/owned'
        with self.assertRaises(APIReadError):
            self.api._sync()
        self.driver.execute_script.return_value['url'] = 'https://myasnb-api-v4.myasnb.com.my/v2/portfolio/other'
        self.api.account = 'owned'
        with self.assertRaises(APIReadError):
            self.api._sync()

    def test_metadata_cache_and_stream_closure(self):
        response = Response({'data': []})
        with patch.object(self.api.session, 'get', return_value=response) as get:
            self.api._get('/v2/subscription/fpxbanks', ttl=3600)
            self.api._get('/v2/subscription/fpxbanks', ttl=3600)
            get.assert_called_once()
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertTrue(get.call_args.kwargs['stream'])
            self.assertTrue(response.closed)
            self.assertEqual(self.api.metrics['cache_hits'], 1)

    def test_429_is_not_ui_fallback_and_no_second_http_request(self):
        with patch.object(self.api.session, 'get', return_value=Response(status=429, headers={'Retry-After': '45'})) as get:
            with self.assertRaises(APIThrottled) as first:
                self.api._get('/v2/settings')
            self.assertEqual(first.exception.seconds, 45)
            with self.assertRaises(APIThrottled):
                self.api._get('/v2/settings')
            get.assert_called_once()

    def test_401_clears_cached_private_data(self):
        self.api.cache['old'] = (0, {'private': 'data'})
        with patch.object(self.api.session, 'get', return_value=Response(status=401)):
            with self.assertRaises(APISessionExpired):
                self.api._get('/v2/settings')
        self.assertFalse(self.api.cache)
        self.assertFalse(self.api.session.cookies)
        self.assertIsNone(self.api.account)

    def test_size_bound_and_error_message_redaction(self):
        response = Response(content=b'x' * (1024 * 1024 + 1))
        with patch.object(self.api.session, 'get', return_value=response):
            with self.assertRaises(APIReadError) as error:
                self.api._get('/v2/settings')
        self.assertTrue(response.closed)
        self.assertNotIn('owned', str(error.exception))
        self.assertNotIn('private-cookie', str(error.exception))

    def test_three_blocked_funds_require_one_portfolio_request(self):
        rows = [{'NAME': name, 'IS_ACTIVE': 4,
                 'IS_ACTIVE_REASON': 'Blocked due to too many retry. Please try again in 5 minutes.'}
                for name in ['ASM', 'ASM2', 'ASM3']]
        with patch.object(self.api.session, 'get', return_value=Response({'data': rows})) as get:
            states = self.api.preflight({name: [name] for name in ['ASM', 'ASM2', 'ASM3']}, 'Bank')
        self.assertEqual(get.call_count, 1)
        self.assertEqual([s['state'] for s in states.values()], ['BLOCKED'] * 3)
        self.assertEqual([s['seconds'] for s in states.values()], [300] * 3)

    def test_closed_channel_and_inactive_bank_preserve_fund_blocks(self):
        rows = [{'NAME': 'ASM', 'FUNDID': 'ASM', 'IS_ACTIVE': 1},
                {'NAME': 'ASM2', 'IS_ACTIVE': 4, 'IS_ACTIVE_REASON': 'Blocked due to retry'}]
        for closed, expected in [(True, 'CHANNEL_CLOSED'), (False, 'BANK_UNAVAILABLE')]:
            data = {'/maintenance': {}, '/v2/settings': {'data': {'subscription_available': not closed}},
                    '/v2/getfundbusinesshour/owned': {'data': [{'desc': 'Fixed Price', 'status': 'Open'}]},
                    '/v2/subscription/fpxbanks': {'data': [{'FULL_NAME': 'Bank', 'IS_ACTIVE': 0}]}}
            self.api.account = 'owned'
            with patch.object(self.api, 'portfolio', return_value=rows), \
                    patch.object(self.api, '_get', side_effect=lambda path, **kwargs: data[path]):
                states = self.api.preflight({'ASM': ['ASM'], 'ASM2': ['ASM2']}, 'Bank')
            self.assertEqual(states['ASM']['state'], expected)
            self.assertEqual(states['ASM2']['state'], 'BLOCKED')

    def test_preflight_reports_static_reasons_for_each_channel_hold(self):
        cases = [({'MAINTENANCE_ACTIVE_PORTAL':True}, {}, [], 1, 'MAINTENANCE'),
                 ({}, {'subscription_available':False}, [], 1, 'SUBSCRIPTIONS_DISABLED'),
                 ({}, {}, [{'desc':'Fixed Price','status':'Closed'}], 1, 'OUTSIDE_BUSINESS_HOURS'),
                 ({}, {}, [], 0, 'BANK_INACTIVE')]
        self.api.account = 'owned'
        for maintenance, settings, hours, bank_active, reason in cases:
            data = {'/maintenance':maintenance, '/v2/settings':{'data':settings},
                    '/v2/getfundbusinesshour/owned':{'data':hours},
                    '/v2/subscription/fpxbanks':{'data':[{'FULL_NAME':'Bank','IS_ACTIVE':bank_active}]}}
            with self.subTest(reason=reason), \
                    patch.object(self.api,'portfolio',return_value=[{'NAME':'ASM','IS_ACTIVE':1}]), \
                    patch.object(self.api,'_get',side_effect=lambda path, **kw:data[path]):
                state = self.api.preflight({'ASM':['ASM']},'Bank')['ASM']
            self.assertEqual(state['reason'],reason)
            self.assertEqual(state['seconds'],60)

    def test_class_b_and_ambiguous_names_do_not_authorize_class_a(self):
        self.assertIsNone(self.api._fund([{'NAME': 'ASM', 'FUNDCLASS': 'B'}], ['ASM']))
        self.assertIsNone(self.api._fund([{'NAME': 'ASM'}, {'NAME': 'ASM'}], ['ASM']))

    def test_unavailable_bank_does_not_stay_cached_for_an_hour(self):
        data = {'/maintenance': {}, '/v2/settings': {'data': {}},
                '/v2/getfundbusinesshour/owned': {'data': []},
                '/v2/subscription/fpxbanks': {'data': [{'FULL_NAME': 'Bank', 'IS_ACTIVE': 0}]}}
        self.api.account = 'owned'
        self.api.cache['/v2/subscription/fpxbanks'] = (0, data['/v2/subscription/fpxbanks'])
        with patch.object(self.api, 'portfolio', return_value=[{'NAME': 'ASM', 'IS_ACTIVE': 1}]), \
                patch.object(self.api, '_get', side_effect=lambda path, **kwargs: data[path]):
            states = self.api.preflight({'ASM': ['ASM']}, 'Bank')
        self.assertEqual(states['ASM']['state'], 'BANK_UNAVAILABLE')
        self.assertNotIn('/v2/subscription/fpxbanks', self.api.cache)

    def test_history_schema_and_reconciliation_never_prove_absence(self):
        old = {'ID': 1, 'TRANSACTION_REF_NO': 'old', 'TRANSACTION_STATUS': 'Successful', 'FUND_ID': 'ASM', 'INV_AMOUNT': '100'}
        new = {**old, 'ID': 2, 'TRANSACTION_REF_NO': 'new', 'TRANSACTION_STATUS': 'Pending'}
        body = {'data': [{'a-date': [old]}]}
        with patch.object(self.api, '_get', return_value=body):
            self.api._sync()
            self.assertEqual(self.api.history(), [old])
        with patch.object(self.api, 'resolve_fund', return_value='ASM'), \
                patch.object(self.api, 'history', return_value=[old]):
            snapshot = self.api.snapshot(['ASM'], '100.00')
            self.assertIn('not proof of failure', self.api.reconcile(snapshot))
        with patch.object(self.api, 'history', return_value=[old, new]):
            result = self.api.reconcile(snapshot)
            self.assertIn('PENDING', result)
            self.assertIn('verify manually', result)
        with patch.object(self.api, 'history', return_value=[new, {**new, 'ID': 3, 'TRANSACTION_REF_NO': 'another'}]):
            self.assertIn('multiple', self.api.reconcile(snapshot))
        with patch.object(self.api, 'history', return_value=[{**new, 'FUND_ID': 'OTHER'}]):
            self.assertIn('no new matching', self.api.reconcile(snapshot))

    def test_malformed_history_is_unavailable_not_empty(self):
        with patch.object(self.api, '_get', return_value={'data': [{'changed-schema': 'value'}]}):
            self.api._sync()
            with self.assertRaises(APIReadError):
                self.api.history()

    def test_main_preflight_marks_all_cooldowns_without_fund_clicks(self):
        self.driver._asnb_api = self.api
        states = {'ASM': {'state': 'BLOCKED', 'seconds': 300}, 'ASM2': {'state': 'BLOCKED', 'seconds': 300}}
        tracker = main.FundCooldownTracker()
        with patch.object(self.api, 'preflight', return_value=states), patch.object(main, 'fund_cooldowns', tracker):
            self.assertEqual(main.api_preflight(self.driver, 'ASM', ['ASM', 'ASM2'], 'Bank'), 'SKIP')
            self.assertTrue(tracker.is_on_cooldown('ASM'))
            self.assertTrue(tracker.is_on_cooldown('ASM2'))
            self.driver.execute_script.assert_not_called()

    def test_main_api_outage_uses_ui_but_expired_session_recovers(self):
        self.driver._asnb_api = self.api
        with patch.object(self.api, 'preflight', side_effect=APIReadError):
            self.assertEqual(main.api_preflight(self.driver, 'ASM', ['ASM'], 'Bank'), 'UI')
        with patch.object(self.api, 'preflight', side_effect=APISessionExpired):
            self.assertEqual(main.api_preflight(self.driver, 'ASM', ['ASM'], 'Bank'), 'SESSION_EXPIRED')

    def test_other_blocked_fund_does_not_trigger_recovery_refresh(self):
        self.driver._asnb_api = self.api
        self.driver._asnb_portfolio_refresh_needed = False
        states = {'ASM': {'state': 'BLOCKED', 'seconds': 300},
                  'ASM2': {'state': 'ELIGIBLE'}}
        with patch.object(self.api, 'preflight', return_value=states), \
                patch.object(main, 'fund_cooldowns', main.FundCooldownTracker()):
            self.assertEqual(main.api_preflight(self.driver, 'ASM2', list(states), 'Bank'), 'READY')
            self.assertFalse(self.driver._asnb_portfolio_refresh_needed)
            states['ASM'] = {'state': 'ELIGIBLE'}
            self.assertEqual(main.api_preflight(self.driver, 'ASM', list(states), 'Bank'), 'READY')
            self.assertTrue(self.driver._asnb_portfolio_refresh_needed)

    def test_ui_block_does_not_refresh_next_eligible_fund(self):
        self.driver._asnb_api = self.api
        self.driver._asnb_portfolio_refresh_needed = False
        main.mark_fund_ui_stale(self.driver, 'ASM2')
        states = {'ASM2': {'state': 'BLOCKED', 'seconds': 300},
                  'ASM3': {'state': 'ELIGIBLE'}}
        with patch.object(self.api, 'preflight', return_value=states), \
                patch.object(main, 'fund_cooldowns', main.FundCooldownTracker()):
            self.assertEqual(main.api_preflight(self.driver, 'ASM3', list(states), 'Bank'), 'READY')
            self.assertFalse(self.driver._asnb_portfolio_refresh_needed)

    def test_refresh_timeout_preserves_session_and_pending_refresh(self):
        from selenium.common.exceptions import TimeoutException
        self.driver._asnb_portfolio_refresh_needed = True
        self.driver._asnb_stale_funds = {'ASM'}
        tracker = main.FundCooldownTracker()
        with patch.object(main, 'WebDriverWait') as wait, \
                patch.object(main, 'fund_cooldowns', tracker):
            wait.return_value.until.side_effect = TimeoutException()
            self.assertFalse(main.refresh_portfolio_ui(self.driver, 'ASM'))
        self.driver.quit.assert_not_called()
        self.assertTrue(self.driver._asnb_portfolio_refresh_needed)
        self.assertEqual(self.driver._asnb_stale_funds, {'ASM'})
        self.assertTrue(tracker.is_on_cooldown('ASM'))

    def test_throttle_applies_to_all_configured_funds(self):
        self.driver._asnb_api = self.api
        tracker = main.FundCooldownTracker()
        with patch.object(self.api, 'preflight', side_effect=APIThrottled(45)), \
                patch.object(main, 'fund_cooldowns', tracker):
            self.assertEqual(main.api_preflight(self.driver, 'ASM', ['ASM', 'ASM2'], 'Bank'), 'SKIP')
            self.assertGreater(tracker.remaining_cooldown('ASM'), 44)
            self.assertGreater(tracker.remaining_cooldown('ASM2'), 44)

    def test_reconciliation_reports_evidence_without_resuming_or_clicking(self):
        self.driver._asnb_api = self.api
        self.driver._asnb_submission_snapshot = {'references': {'before'}, 'fund_id': 'ASM', 'amount': 100}
        with patch.object(self.api, 'reconcile', return_value='one new matching record (PENDING)') as reconcile, \
                patch.object(main, 'wait_for_submission_review') as review:
            main.reconcile_submission(self.driver)
            reconcile.assert_called_once_with(self.driver._asnb_submission_snapshot)
            self.driver.execute_script.assert_not_called()
            review.assert_not_called()  # Caller still owns the mandatory manual gate.
