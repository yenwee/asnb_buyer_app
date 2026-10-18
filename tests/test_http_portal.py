import base64
import copy
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

from asnb.api import APIReadError
from asnb.handoff import HandoffError, import_http_session
from asnb.http_portal import HTTPPortal, LoginError
from tests.test_handoff import fixture


class Response:
    def __init__(self, data, status=200):
        self.status_code = status
        self.headers = {}
        self.raw = json.dumps(data).encode()
        self.closed = False

    def iter_content(self, size):
        yield self.raw

    def close(self):
        self.closed = True


class HTTPPortalTests(unittest.TestCase):
    def setUp(self):
        self.api = HTTPPortal()
        self.addCleanup(self.api.close)

    def test_login_has_no_browser_dependency_and_validates_account(self):
        with patch.object(self.api, '_login_material', return_value=b'dummy-public-test-key'), \
                patch.object(self.api, '_post', side_effect=[
                    ({'data': {'security_phrase': 'Demo'}, 'errors': {'ReferenceId':'trace'}}, None),
                    ({'token':'private-token', 'uhid':'DEMO'}, None)]) as post, \
                patch.object(self.api, '_get', return_value={'data': {'UNITHOLDERID':'DEMO','FIRSTNAME':'Demo'}}), \
                patch.object(self.api, 'portfolio', return_value=[]):
            self.assertTrue(self.api.login('demo-user','private-password'))
        jwt = post.call_args_list[1].args[1]['jwt']
        claims = json.loads(base64.urlsafe_b64decode(jwt.split('.')[1] + '==='))
        self.assertEqual(claims['sub'], 'demo-user')
        self.assertEqual(claims['password'], 'private-password')
        self.assertEqual(claims['deviceId'], '')
        self.assertEqual(claims['exp'] - claims['iat'], 60)
        self.assertEqual(self.api.storage['UHID'], 'DEMO')
        self.assertIsNone(self.api.driver)

    def test_login_challenge_does_not_retry(self):
        with patch.object(self.api, '_login_material', return_value=b'dummy'), \
                patch.object(self.api, '_post', side_effect=[
                    ({'data': {'security_phrase':'Demo'}}, None),
                    ({'errors': {'102':'active session'}}, None)]) as post:
            with self.assertRaises(LoginError):
                self.api.login('demo','private')
        self.assertEqual(post.call_count, 2)  # phrase + one credential request


    def test_post_has_no_redirect_or_transport_retry(self):
        self.api.session.post = Mock(return_value=Response({}, status=503))
        with self.assertRaises(APIReadError):
            self.api._post('/v2/login', {'jwt':'private'})
        self.api.session.post.assert_called_once()
        self.assertFalse(self.api.session.post.call_args.kwargs['allow_redirects'])

    def test_json_wire_body_matches_browser_and_content_length_counts_utf8_bytes(self):
        self.api.session.post = Mock(return_value=Response({}))
        payload = {'username': 'Dummy 用户', 'enabled': True}
        self.api._post('/login/securephrase', payload)
        args = self.api.session.post.call_args
        options = args.kwargs
        self.assertNotIn('json', options)
        self.assertEqual(options['data'], '{"username":"Dummy 用户","enabled":true}'.encode())
        prepared = self.api.session.prepare_request(requests.Request(
            'POST', args.args[0], data=options['data'], headers=options['headers']))
        self.assertEqual(prepared.headers['Content-Type'], 'application/json')
        self.assertEqual(int(prepared.headers['Content-Length']), len(prepared.body))
        self.assertNotIn('Transfer-Encoding', prepared.headers)

    def test_524_records_safe_metadata_and_stays_locked_without_retry(self):
        _, context = fixture()
        self.api.account, self.api.authenticated = 'DEMO', True
        response = Response({}, status=524)
        response.headers = {'Server':'ESA', 'Content-Type':'text/html; charset=utf-8',
                            'Set-Cookie':'private-cookie', 'CF-Ray':'private-trace',
                            'X-Arbitrary':'private-header'}
        self.api.session.post = Mock(return_value=response)
        with self.assertRaises(HandoffError):
            self.api.purchase(context)
        self.api.session.post.assert_called_once()
        self.assertTrue(self.api.submission_locked)
        self.assertTrue(response.closed)
        diagnostic = self.api.last_write
        self.assertEqual(diagnostic['http'], 524)
        self.assertEqual(diagnostic['edge'], 'esa')
        self.assertEqual(diagnostic['response_type'], 'text/html')
        self.assertTrue(diagnostic['cf_ray_present'])
        self.assertGreater(diagnostic['request_bytes'], 0)
        self.assertGreaterEqual(diagnostic['elapsed_ms'], diagnostic['response_headers_ms'])
        self.assertNotIn('private', self.api.purchase_diagnostic())
        with self.assertRaises(HandoffError):
            self.api.purchase(context)
        self.api.session.post.assert_called_once()

    def test_post_rejects_arbitrary_routes_before_network(self):
        self.api.session.post = Mock()
        for route in ('/v2/payment', '/v2/subscription/provisional/OTHER/FUND', '//evil.invalid/'):
            with self.assertRaises(APIReadError):
                self.api._post(route, {})
        self.api.session.post.assert_not_called()

    def test_524_does_not_enter_json_response_parser(self):
        response = Response({}, status=524)
        response.raw = b''
        self.api.session.post = Mock(return_value=response)
        with patch('asnb.http_portal.json.loads') as parser:
            with self.assertRaisesRegex(APIReadError, 'HTTP 524 received'):
                self.api._post('/v2/login', {})
        parser.assert_not_called()
        self.assertEqual(self.api.last_write['response_bytes'], 0)

    def test_gateway_reference_id_does_not_turn_068_into_unknown_order(self):
        _, context = fixture()
        self.api.account, self.api.authenticated = 'DEMO', True
        rejected = {'data': {'TRANSACTIONSTATUS':'Reject','REJECTCODE':'068'},
                    'errors': {'ReferenceId':'private-trace-id'}}
        self.api.session.post = Mock(return_value=Response(rejected))
        self.assertEqual(self.api.purchase(context), 'INSUFFICIENT')
        self.assertFalse(self.api.submission_locked)
        diagnostic = self.api.purchase_diagnostic()
        self.assertIn('"http":200',diagnostic)
        self.assertIn('"reject_code":"068"',diagnostic)
        self.assertNotIn('private-trace-id',diagnostic)

    def test_gateway_reference_id_on_valid_success_and_signed_values_not_logged(self):
        response, context = fixture()
        response['errors'] = {'ReferenceId':'private-trace-id'}
        self.api.account, self.api.authenticated = 'DEMO', True
        self.api.session.post = Mock(return_value=Response(response))
        self.assertEqual(self.api.purchase(context), 'SUCCESS')
        diagnostic = self.api.purchase_diagnostic()
        self.assertNotIn('DUMMY',diagnostic)
        self.assertNotIn('private-trace-id',diagnostic)

    def test_network_error_diagnostic_preserves_exception_type_without_request(self):
        _, context = fixture()
        self.api.account, self.api.authenticated = 'DEMO', True
        self.api.session.post = Mock(side_effect=requests.ReadTimeout('private URL and token'))
        with self.assertRaises(HandoffError):self.api.purchase(context)
        diagnostic = self.api.purchase_diagnostic()
        self.assertIn('ReadTimeout',diagnostic)
        self.assertNotIn('private URL',diagnostic)
        self.assertTrue(self.api.submission_locked)

    def test_balance_summary_uses_portal_values_and_units(self):
        self.api.portfolio_value = '12345.60'
        self.api.portfolio_rows = [{'SHORT_NAME':'ASM 3','UHHOLDINGS':1000,'TOTALUNITS':1000}]
        lines = self.api.balance_lines()
        self.assertIn('RM 12,345.60',lines[0])
        self.assertEqual(lines[1], '  ASM 3: RM 1,000.00 | 1,000.00 units')

    def test_balance_summary_filters_to_requested_funds(self):
        self.api.portfolio_rows = [
            {'NAME':'Amanah Saham Malaysia','SHORT_NAME':'ASM','UHHOLDINGS':100,'TOTALUNITS':100,'FUNDCLASS':'A'},
            {'NAME':'Amanah Saham Malaysia 2 - Wawasan','SHORT_NAME':'ASM 2','UHHOLDINGS':200,'TOTALUNITS':200,'FUNDCLASS':'A'},
            {'NAME':'Amanah Saham Malaysia 3','SHORT_NAME':'ASM 3','UHHOLDINGS':300,'TOTALUNITS':300,'FUNDCLASS':'A'},
            {'NAME':'Other Fund','SHORT_NAME':'OTHER','UHHOLDINGS':400,'TOTALUNITS':400,'FUNDCLASS':'A'}]
        lines = self.api.balance_lines({'ASM':['Amanah Saham Malaysia'],
            'ASM 2':['Amanah Saham Malaysia 2 - Wawasan'], 'ASM 3':['Amanah Saham Malaysia 3']})
        self.assertEqual(len(lines),4)  # Portfolio total + three selected fund balances.
        self.assertNotIn('OTHER','\n'.join(lines))

    def test_asm2_context_resolves_wawasan_alias_to_asw_without_submission(self):
        self.api.account, self.api.authenticated = 'DEMO', True
        row = {'NAME':'Amanah Saham Malaysia 2 - Wawasan', 'SHORT_NAME':'ASM 2 Wawasan',
               'FUNDID':'ASW', 'FUNDCLASS':'A', 'IS_ACTIVE':1, 'IS_FIXED':1}
        with patch.object(self.api, 'portfolio', return_value=[row]), \
                patch.object(self.api, '_get', return_value={'data':[
                    {'FPX_BANK_NAME':'Hong Leong Bank','FPX_BANK_CODE':'HLB0224','IS_ACTIVE':1}]}), \
                patch.object(self.api.session, 'post') as post:
            context = self.api.context('Amanah Saham Malaysia 2', '5000', 'Hong Leong Bank',
                fund_aliases=['Amanah Saham Malaysia 2', 'Amanah Saham Malaysia 2 - Wawasan'])
        self.assertEqual(context['fund_id'], 'ASW')
        self.assertEqual(context['bankid'], 'HLB0224')
        post.assert_not_called()

    def test_purchase_response_timeout_is_longer_than_authentication_timeout(self):
        self.api.account = 'DEMO'
        self.api.session.post = Mock(return_value=Response({'data':{}}))
        self.api._post('/v2/subscription/provisional/DEMO/AS1M', {})
        self.assertEqual(self.api.session.post.call_args.kwargs['timeout'],(5,60))
        self.api.session.post.return_value = Response({'data':{}})
        self.api._post('/v2/login', {})
        self.assertEqual(self.api.session.post.call_args.kwargs['timeout'],(5,20))

    def test_explicit_068_retries_but_success_and_timeout_stay_locked(self):
        response, context = fixture()
        self.api.account, self.api.authenticated = 'DEMO', True
        rejected = {'data': {'TRANSACTIONSTATUS':'Reject','REJECTCODE':'068'}}
        with patch.object(self.api, '_post', side_effect=[(rejected,None),(response,None)]) as post:
            self.assertEqual(self.api.purchase(context), 'INSUFFICIENT')
            self.assertFalse(self.api.submission_locked)
            self.assertEqual(self.api.purchase(context), 'SUCCESS')
            self.assertTrue(self.api.submission_locked)
            with self.assertRaises(HandoffError):
                self.api.purchase(context)
        self.assertEqual(post.call_count, 2)

    def test_timeout_or_bad_response_is_not_replayed_after_reset(self):
        _, context = fixture()
        self.api.account, self.api.authenticated = 'DEMO', True
        with patch.object(self.api, '_post', side_effect=TimeoutError()) as post:
            with self.assertRaises(HandoffError):
                self.api.purchase(context)
        self.api.reset()
        self.assertTrue(self.api.submission_locked)
        with self.assertRaises(LoginError):
            self.api.login('demo','private')
        post.assert_called_once()

    def test_logout_replays_old_cookie_to_verify_invalidation(self):
        self.api.account, self.api.authenticated = 'DEMO', True
        self.api.session.cookies.set('TokenID','private-old-cookie', domain='.myasnb.com.my',path='/')
        old = MagicMock()
        old.cookies = requests.cookies.RequestsCookieJar()
        old.get.return_value = Response({}, status=401)
        old.__enter__.return_value = old
        def logout(*args):
            self.api.session.cookies.clear()
            return {},None
        with patch.object(self.api, '_post', side_effect=logout), \
                patch('asnb.http_portal.requests.Session', return_value=old):
            result = self.api.logout(verify=True)
        self.assertTrue(result['oldSessionRejected'])
        self.assertEqual(result['postLogoutHTTP'],401)
        self.assertEqual(old.cookies.get('TokenID'),'private-old-cookie')
        self.assertFalse(self.api.authenticated)
        self.assertEqual(len(self.api.session.cookies),0)

    def test_logout_200_is_not_alone_proof_of_invalidation(self):
        self.api.account, self.api.authenticated = 'DEMO', True
        old = MagicMock()
        old.__enter__.return_value = old
        old.get.return_value = Response({'data':{'UNITHOLDERID':'DEMO'}})
        with patch.object(self.api,'_post',return_value=({},None)), \
                patch('asnb.http_portal.requests.Session',return_value=old):
            result = self.api.logout(verify=True)
        self.assertFalse(result['oldSessionRejected'])
        self.assertTrue(result['oldSessionStillAuthenticated'])

    def test_cookie_attributes_and_pre_bootstrap_storage_are_preserved(self):
        self.api.account, self.api.authenticated = 'DEMO', True
        self.api.storage = {'isLoggedIn':'true','UHID':'DEMO'}
        cookie = requests.cookies.create_cookie('TokenID','private',domain='.myasnb.com.my',path='/',
            secure=True,rest={'HttpOnly':None,'SameSite':'Strict'})
        self.api.session.cookies.set_cookie(cookie)
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {'identifier':'init'}
        driver.execute_async_script.return_value = True
        import_http_session(driver,self.api)
        calls = driver.execute_cdp_cmd.call_args_list
        transferred = calls[1].args[1]['cookies'][0]
        self.assertTrue(transferred['httpOnly'])
        self.assertTrue(transferred['secure'])
        self.assertEqual(transferred['sameSite'],'Strict')
        driver.get.assert_called_once_with('https://www.myasnb.com.my/portfolio')
        self.assertTrue(any(c.args[0]=='Page.removeScriptToEvaluateOnNewDocument' for c in calls))
        driver.find_element.assert_not_called()
