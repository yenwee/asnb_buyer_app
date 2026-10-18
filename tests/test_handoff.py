import copy
import unittest
from unittest.mock import Mock, patch

from asnb.handoff import FPX_FIELDS, HandoffError, confirmation_state, open_confirmation
from asnb.api_purchase import submit_api_and_restore, purchase_context
from asnb.api import PortalAPI


def fixture():
    context = dict(account='DEMO', fund_id='AS1M', fund_name='Amanah Saham Malaysia 3',
                   fundshortname='ASM 3', bankid='DEMO-BANK', bankname='Hong Leong Bank',
                   amount='5000', isFixed=1, fundClass='A', isPEP='false')
    fpx = dict.fromkeys(FPX_FIELDS, 'DUMMY')
    fpx.update(fpx_buyerBankId='DEMO-BANK', fpx_txnAmount='5000.00', fpx_txnCurrency='MYR')
    response = {'data': dict(TRANSACTIONSTATUS='Successful', UNITHOLDERID='DEMO',
        FUNDID='AS1M', FUNDCLASS='A', FIRSTNAME='Demo Investor', AMOUNTAPPLIED=5000,
        TRANSACTIONNUMBER='DEMO-TRANSACTION', TRANSACTIONDATE='20261018',
        TRANSACTIONTIME='120704', FUNDPRICE=1, UNITSALLOTED=5000,
        SALESCHARGE=0, GSTAMOUNT=0, FPX_URL='https://example.invalid/payment', FPX_DATA=fpx),
        'errors': {}}
    return response, context


def metadata_driver(context):
    driver = Mock(_asnb_api_purchase_dispatched=False)
    driver.execute_script.return_value = {'account': context.get('account'), 'isPEP': context.get('isPEP')}
    api = PortalAPI(driver)
    driver._asnb_api = api
    api.account = context.get('account')
    api.portfolio = Mock(return_value=[dict(NAME=context.get('fund_name'), FUNDID=context.get('fund_id'),
        SHORT_NAME=context.get('fundshortname'), IS_FIXED=context.get('isFixed'),
        FUNDCLASS=context.get('fundClass'), IS_ACTIVE=1)])
    api._get = Mock(return_value={'data': [dict(FPX_BANK_NAME=context.get('bankname'),
        FPX_BANK_CODE=context.get('bankid'), IS_ACTIVE=1)]})
    return driver


class HandoffTests(unittest.TestCase):
    def test_main_api_failure_does_not_fall_back_to_ui_submission(self):
        from asnb import main
        driver = Mock(_asnb_purchase_transport='api')
        with patch.object(main, 'wait_for_element', return_value=Mock()) as form_element, \
                patch.object(main, 'Select', return_value=Mock(options=[1, 2])), \
                patch.object(main, 'human_type'), patch.object(main, 'between_actions'), \
                patch.object(main, 'wait_for_clickable_element') as clickable, \
                patch.object(main, 'submit_and_observe') as ui_submit, \
                patch('asnb.api_purchase.submit_api_and_restore', side_effect=HandoffError('manual review')) as api_submit:
            self.assertEqual(main.purchase_unit(driver, 'ASM 3', '5000', 'Hong Leong Bank'), 'UNCERTAIN')
        form_element.assert_not_called()
        clickable.assert_not_called()
        ui_submit.assert_not_called()
        api_submit.assert_called_once()

    def test_maps_frontend_state_and_flattens_all_fpx_fields(self):
        response, context = fixture()
        state = confirmation_state(response, context)
        self.assertEqual(state['fundshortname'], 'ASM 3')
        self.assertEqual(state['bankname'], 'Hong Leong Bank')
        self.assertEqual(state['AMOUNTAPPLIED'], 5000)
        self.assertEqual(state['txnType'], 'ADD')
        self.assertEqual(state['fundClass'], 'A')
        self.assertIsNone(state['FUND_NAME'])  # Absent in the actual capture too.
        self.assertNotIn('FPX_DATA', state)
        for field in FPX_FIELDS:
            self.assertEqual(state[field], response['data']['FPX_DATA'][field])

    def test_invalid_responses_never_reach_browser(self):
        response, context = fixture()
        for change in (
            lambda r: r.update(data='malformed'),
            lambda r: r['data'].pop('FPX_URL'),
            lambda r: r['data'].pop('FPX_DATA'),
            lambda r: r['data']['FPX_DATA'].pop('fpx_checkSum'),
            lambda r: r['data'].update(TRANSACTIONSTATUS='Rejected'),
            lambda r: r['data'].update(AMOUNTAPPLIED='NaN'),
            lambda r: r['data'].update(UNITHOLDERID='OTHER'),
            lambda r: r['data'].update(FUNDID='OTHER'),
            lambda r: r['data']['FPX_DATA'].update(fpx_buyerBankId='OTHER'),
            lambda r: r['data']['FPX_DATA'].update(fpx_txnAmount='6000'),
            lambda r: r['data']['FPX_DATA'].update(fpx_checkSum=''),
            lambda r: r.update(errors={'CODE': 'ERROR'}),
        ):
            with self.subTest(change=change):
                bad = copy.deepcopy(response)
                change(bad)
                with self.assertRaises(HandoffError):
                    confirmation_state(bad, context)
        with self.assertRaises(HandoffError):
            confirmation_state(response, None)

    def test_expired_session_does_not_navigate_or_retry(self):
        response, context = fixture()
        driver = Mock()
        driver.execute_script.return_value = False
        with self.assertRaisesRegex(HandoffError, 'Session expired'):
            open_confirmation(driver, confirmation_state(response, context), context['account'])
        driver.execute_script.assert_called_once()
        driver.execute_async_script.assert_not_called()

    def test_missing_state_never_calls_browser(self):
        driver = Mock()
        with self.assertRaisesRegex(HandoffError, 'Missing confirmation state'):
            open_confirmation(driver, None, 'DEMO')
        driver.execute_script.assert_not_called()

    def test_fresh_success_issues_once_and_restores_unchecked_confirmation(self):
        response, context = fixture()
        driver = metadata_driver(context)
        self.addCleanup(driver._asnb_api.close)
        driver.execute_async_script.return_value = {'response': response}
        with patch('asnb.api_purchase.open_confirmation') as restore:
            self.assertEqual(submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname']), 'SUCCESS')
            driver.execute_async_script.assert_called_once()
            state = restore.call_args.args[1]
            self.assertEqual(state['fundshortname'], 'ASM 3')
            self.assertNotIn('agree', state)
        with self.assertRaisesRegex(HandoffError, 'already dispatched'):
            submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname'])
        driver.execute_async_script.assert_called_once()

    def test_failed_acknowledgement_never_replays_or_falls_back(self):
        _, context = fixture()
        for result in ({'error': 'uncertain'}, {'error': 'http_error'}, {'response': {'data': 'bad'}}):
            with self.subTest(result=result):
                driver = metadata_driver(context)
                self.addCleanup(driver._asnb_api.close)
                driver.execute_async_script.return_value = result
                with patch('asnb.api_purchase.open_confirmation') as restore:
                    with self.assertRaises(HandoffError):
                        submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname'])
                    restore.assert_not_called()
                with self.assertRaises(HandoffError):
                    submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname'])
                driver.execute_async_script.assert_called_once()

    def test_explicit_insufficient_rejection_allows_next_attempt_then_success(self):
        response, context = fixture()
        driver = metadata_driver(context)
        self.addCleanup(driver._asnb_api.close)
        rejected = {'data': {'TRANSACTIONSTATUS': 'Reject', 'REJECTCODE': '068'}, 'errors': {}}
        driver.execute_async_script.side_effect = [{'response': rejected}, {'response': response}]
        with patch('asnb.api_purchase.open_confirmation') as restore:
            self.assertEqual(submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname']), 'INSUFFICIENT')
            self.assertFalse(driver._asnb_api_purchase_dispatched)
            restore.assert_not_called()
            self.assertEqual(submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname']), 'SUCCESS')
            restore.assert_called_once()
        self.assertTrue(driver._asnb_api_purchase_dispatched)
        self.assertEqual(driver.execute_async_script.call_count, 2)

    def test_explicit_block_rejection_returns_server_cooldown(self):
        _, context = fixture()
        driver = metadata_driver(context)
        self.addCleanup(driver._asnb_api.close)
        driver.execute_async_script.return_value = {'response': {'data': {
            'TRANSACTIONSTATUS': 'Rejected', 'REJECTCODE': '1001',
            'REJECTREASON': 'Try again in 5 minutes'}, 'errors': {}}}
        self.assertEqual(submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname']), 'BLOCKED')
        self.assertEqual(driver._asnb_block_retry_seconds, 300)
        self.assertFalse(driver._asnb_api_purchase_dispatched)

    def test_other_or_contradictory_rejections_stay_latched(self):
        _, context = fixture()
        for data in (
            {'TRANSACTIONSTATUS': 'Reject', 'REJECTCODE': '999'},
            {'TRANSACTIONSTATUS': 'Pending', 'REJECTCODE': '068'},
            {'TRANSACTIONSTATUS': 'Successful', 'REJECTCODE': '068'},
            {'TRANSACTIONSTATUS': 'Reject', 'REJECTCODE': '068', 'FPX_DATA': {'unexpected': 'data'}},
        ):
            driver = metadata_driver(context)
            self.addCleanup(driver._asnb_api.close)
            driver.execute_async_script.return_value = {'response': {'data': data}}
            with self.assertRaises(HandoffError):
                submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname'])
            self.assertTrue(driver._asnb_api_purchase_dispatched)

    def test_api_retry_loop_skips_all_purchase_form_and_popup_controls(self):
        from asnb import main
        driver = Mock(_asnb_purchase_transport='api')
        with patch('asnb.api_purchase.submit_api_and_restore', side_effect=['INSUFFICIENT', 'BLOCKED']) as submit, \
                patch.object(main, 'wait_for_element') as form, \
                patch.object(main, 'wait_for_clickable_any') as popup, \
                patch.object(main, 'wait_for_clickable_element') as button, \
                patch.object(main.time, 'sleep') as sleep:
            self.assertEqual(main.purchase_unit(driver, 'ASM 3', '5000', 'Hong Leong Bank'), 'BLOCKED')
        self.assertEqual(submit.call_count, 2)
        sleep.assert_called_once()
        form.assert_not_called()
        popup.assert_not_called()
        button.assert_not_called()

    def test_preflight_mismatches_never_submit(self):
        _, context = fixture()
        for changes in ({'account': None}, {'isPEP': 'true'}, {'fundClass': 'B'}, {'isFixed': 0},
                        {'bankname': 'Other'}, {'bankid': None}, {'fundshortname': None}):
            with self.subTest(changes=changes):
                driver = metadata_driver(dict(context, **changes))
                self.addCleanup(driver._asnb_api.close)
                with self.assertRaises(HandoffError):
                    submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname'])
                driver.execute_async_script.assert_not_called()

    def test_context_uses_api_metadata_without_any_purchase_form(self):
        _, context = fixture()
        driver = metadata_driver(context)
        self.addCleanup(driver._asnb_api.close)
        result = purchase_context(driver, context['fund_name'], '5000', context['bankname'])
        self.assertEqual(result['bankid'], 'DEMO-BANK')
        self.assertEqual(result['fund_id'], 'AS1M')
        self.assertEqual(result['amount'], '5000')
        driver.find_element.assert_not_called()
        driver.execute_async_script.assert_not_called()
        driver._asnb_api.portfolio.assert_called_once_with(force=True)
        driver._asnb_api._get.assert_called_once_with('/v2/subscription/fpxbanks', ttl=0)

    def test_handoff_failure_keeps_request_latched(self):
        response, context = fixture()
        driver = metadata_driver(context)
        self.addCleanup(driver._asnb_api.close)
        driver.execute_async_script.return_value = {'response': response}
        with patch('asnb.api_purchase.open_confirmation', side_effect=HandoffError('not rendered')):
            with self.assertRaises(HandoffError):
                submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname'])
        self.assertTrue(driver._asnb_api_purchase_dispatched)
        driver.execute_async_script.assert_called_once()
