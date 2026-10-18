"""API submission followed by a manual Chrome handoff.

Legacy browser-backed helper. The default runner uses api_runner/HTTPPortal.
Tests mock every submission.
The read-only PortalAPI adapter remains read-only.
"""

from .handoff import HandoffError, PORTAL, confirmation_state, money, open_confirmation, response_errors


def purchase_context(driver, fund_name, amount, bank_name):
    """Resolve configured choices through read-only APIs; no purchase form needed."""
    from .api import PortalAPI, get_api, _name
    session = driver.execute_script("""
        if (location.origin !== arguments[0] || location.pathname !== '/portfolio' ||
            sessionStorage.getItem('isLoggedIn') !== 'true') return null;
        return {account: sessionStorage.getItem('UHID'), isPEP: sessionStorage.getItem('isPEP')};
    """, PORTAL)
    if not isinstance(session, dict) or not session.get('account'):
        raise HandoffError('Authenticated portfolio session unavailable; no submission.')
    if session.get('isPEP') not in (None, 'false'):
        raise HandoffError('Active PEP flow requires manual review; no submission.')
    client = get_api(driver)
    temporary = client is None
    if temporary:
        client = PortalAPI(driver)
    try:
        fund = client._fund(client.portfolio(force=True), [fund_name])
        if client.account != session['account']:
            raise HandoffError('API account mismatch; no submission.')
        if not fund or fund.get('IS_FIXED') != 1 or fund.get('FUNDCLASS') != 'A' or fund.get('IS_ACTIVE') != 1:
            raise HandoffError('Eligible fixed-price Class A fund unavailable; no submission.')
        banks = client._get('/v2/subscription/fpxbanks', ttl=0).get('data')
        if not isinstance(banks, list):
            raise HandoffError('Bank metadata unavailable; no submission.')
        matches = [bank for bank in banks if isinstance(bank, dict) and
                   _name(bank.get('FPX_BANK_NAME')) == _name(bank_name)]
        if len(matches) != 1 or matches[0].get('IS_ACTIVE') == 0 or not matches[0].get('FPX_BANK_CODE'):
            raise HandoffError('Configured FPX bank unavailable or ambiguous; no submission.')
        bank = matches[0]
        if not fund.get('FUNDID') or not fund.get('SHORT_NAME'):
            raise HandoffError('Fund metadata unavailable; no submission.')
        return dict(account=session['account'], fund_id=fund['FUNDID'],
                    fundshortname=fund['SHORT_NAME'], img=fund.get('FUND_LOGO'),
                    isFixed=1, bankid=bank['FPX_BANK_CODE'], bankname=bank['FPX_BANK_NAME'],
                    amount=str(money(amount)))
    except HandoffError:
        raise
    except Exception:
        raise HandoffError('Read-only purchase metadata unavailable; no submission.') from None
    finally:
        if temporary:
            client.close()


def submit_api_and_restore(driver, fund_name, amount, bank_name, referral_code=''):
    """One POST per call. Only explicit 068/1001 rejection releases the latch."""
    if getattr(driver, '_asnb_api_purchase_dispatched', False) is True:
        raise HandoffError('An API purchase was already dispatched in this browser; no automatic retry.')
    context = purchase_context(driver, fund_name, amount, bank_name)
    print('API purchase attempt from the authenticated portfolio session.')
    print('Initial investment terms stay accepted; final payment agreement remains manual.')
    # Mark before dispatch: timeouts and lost acknowledgements can still mean acceptance.
    driver._asnb_api_purchase_dispatched = True
    try:
        driver.set_script_timeout(30)
        result = driver.execute_async_script("""
            const expected = arguments[0], referral = arguments[1], done = arguments[arguments.length - 1];
            if (location.origin !== 'https://www.myasnb.com.my' || location.pathname !== '/portfolio' ||
                sessionStorage.getItem('isLoggedIn') !== 'true' ||
                sessionStorage.getItem('UHID') !== expected.account ||
                !/^[A-Za-z0-9_-]{1,128}$/.test(expected.account) ||
                !/^[A-Za-z0-9_-]{1,128}$/.test(expected.fund_id)) {
                done({error: 'context_changed'}); return;
            }
            sessionStorage.setItem('TransactionType', 'ADD');
            sessionStorage.setItem('FUNDID', expected.fund_id);
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 20000);
            const body = {amount: expected.amount.replaceAll(',', ''), fpxBankId: expected.bankid,
                TXN_CODE: '101', CRITERIACONFIRM: true, PROSPECTUSAGREE: true,
                riskSrcOfFund: '', riskSrcOfWealth: '', riskOtherSrcOfFund: '',
                riskOtherSrcOfWealth: '', referral_code: referral};
            fetch('https://myasnb-api-v4.myasnb.com.my/v2/subscription/provisional/' +
                encodeURIComponent(expected.account) + '/' + encodeURIComponent(expected.fund_id), {
                method: 'POST', credentials: 'include', redirect: 'error',
                headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
                body: JSON.stringify(body), signal: controller.signal
            }).then(async response => {
                if (response.status !== 200) {done({error: 'http_error'}); return;}
                const text = await response.text();
                if (text.length > 1048576) {done({error: 'oversized_response'}); return;}
                done({response: JSON.parse(text)});
            }).catch(() => done({error: 'uncertain'})).finally(() => clearTimeout(timer));
        """, context, referral_code)
        if not isinstance(result, dict) or result.get('error') or 'response' not in result:
            raise HandoffError('API outcome uncertain or rejected; no automatic retry. Review history manually.')
        response = result['response']
        data = response.get('data') if isinstance(response, dict) else None
        if (isinstance(data, dict) and not response.get('error') and not response_errors(response)
                and not (isinstance(response.get('meta'), dict) and response['meta'].get('Error'))
                and data.get('TRANSACTIONSTATUS') in ('Reject', 'Rejected') and not data.get('FPX_DATA')):
            code = str(data.get('REJECTCODE', '')).strip()
            if code in ('068', '1001'):
                if code == '1001':
                    from .purchase_state import block_retry_seconds
                    driver._asnb_block_retry_seconds = block_retry_seconds(str(data.get('REJECTREASON', '')))
                driver._asnb_api_purchase_dispatched = False
                return 'INSUFFICIENT' if code == '068' else 'BLOCKED'
        state = confirmation_state(response, context)
        open_confirmation(driver, state, context['account'])
        return 'SUCCESS'
    except HandoffError:
        raise
    except Exception:
        raise HandoffError('API attempt or Chrome handoff unverified; no automatic retry. Review history manually.') from None
