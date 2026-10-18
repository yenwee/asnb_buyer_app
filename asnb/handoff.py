"""Validate a fresh subscription response and open the manual confirmation step.

No payment URL is opened here. Never load an old private capture into a live tab.
"""

from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from selenium.webdriver.support.ui import WebDriverWait

PORTAL = 'https://www.myasnb.com.my'
FPX_FIELDS = (
    'fpx_buyerAccNo', 'fpx_buyerBankBranch', 'fpx_buyerBankId',
    'fpx_buyerEmail', 'fpx_buyerIban', 'fpx_buyerId', 'fpx_buyerName',
    'fpx_makerName', 'fpx_msgToken', 'fpx_msgType', 'fpx_productDesc',
    'fpx_sellerBankCode', 'fpx_sellerExId', 'fpx_sellerExOrderNo',
    'fpx_sellerId', 'fpx_sellerOrderNo', 'fpx_sellerTxnTime',
    'fpx_txnAmount', 'fpx_txnCurrency', 'fpx_version', 'fpx_checkSum',
)
RESPONSE_FIELDS = (
    'UNITHOLDERID', 'GSTAMOUNT', 'FUNDID', 'FUND_NAME', 'FIRSTNAME',
    'SALESCHARGE', 'AMOUNTAPPLIED', 'UNITSALLOTED', 'TRANSACTIONNUMBER',
    'FUNDPRICE', 'TRANSACTIONDATE', 'TRANSACTIONTIME', 'FPX_URL',
)


class HandoffError(ValueError):
    """Static diagnostics only; never expose signed FPX data or personal details."""


def response_errors(response):
    """ReferenceId is gateway trace metadata, not a business rejection."""
    errors = response.get('errors') if isinstance(response, dict) else None
    if isinstance(errors, dict):
        return {key: value for key, value in errors.items() if key != 'ReferenceId' and value is not None}
    return errors


def money(value):
    try:
        value = Decimal(str(value).replace(',', ''))
        if value.is_finite() and value > 0:
            return value
    except (InvalidOperation, ValueError):
        pass
    raise HandoffError('Invalid investment amount; manual review required.')


def confirmation_state(response, context):
    if not isinstance(context, dict) or not all(context.get(k) for k in
            ('account', 'fund_id', 'fundshortname', 'bankid', 'bankname', 'amount')):
        raise HandoffError('Missing navigation context; manual review required.')
    if not isinstance(response, dict) or response_errors(response) or response.get('error'):
        raise HandoffError('Malformed subscription response; manual review required.')
    data = response.get('data')
    if not isinstance(data, dict) or data.get('TRANSACTIONSTATUS') != 'Successful':
        raise HandoffError('Subscription was not accepted; no automatic retry.')
    if str(data.get('UNITHOLDERID')) != str(context['account']):
        raise HandoffError('Account mismatch; manual review required.')
    if data.get('FUNDID') != context['fund_id'] or money(data.get('AMOUNTAPPLIED')) != money(context['amount']):
        raise HandoffError('Fund or amount mismatch; manual review required.')
    url = data.get('FPX_URL')
    if not isinstance(url, str) or urlsplit(url).scheme != 'https' or not urlsplit(url).hostname:
        raise HandoffError('Missing or invalid FPX URL; manual review required.')
    fpx = data.get('FPX_DATA')
    if not isinstance(fpx, dict) or any(not isinstance(fpx.get(k), str) for k in FPX_FIELDS):
        raise HandoffError('Missing or malformed FPX fields; manual review required.')
    required = ('fpx_buyerBankId', 'fpx_sellerOrderNo', 'fpx_sellerExOrderNo',
                'fpx_sellerId', 'fpx_sellerExId', 'fpx_sellerTxnTime',
                'fpx_msgToken', 'fpx_msgType', 'fpx_txnCurrency', 'fpx_version', 'fpx_checkSum')
    if any(not fpx[k].strip() for k in required):
        raise HandoffError('Missing FPX payment identifiers; manual review required.')
    if fpx['fpx_buyerBankId'] != context['bankid'] or money(fpx['fpx_txnAmount']) != money(context['amount']):
        raise HandoffError('FPX bank or amount mismatch; manual review required.')
    if fpx['fpx_txnCurrency'] != 'MYR':
        raise HandoffError('Unexpected FPX currency; manual review required.')
    for key in ('FIRSTNAME', 'TRANSACTIONNUMBER', 'TRANSACTIONDATE', 'TRANSACTIONTIME'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise HandoffError('Missing confirmation details; manual review required.')
    state = {k: data.get(k) for k in RESPONSE_FIELDS}
    # This captured API response omits FUND_NAME; the portal displays fundshortname.
    state.update({k: context.get(k) for k in ('fundshortname', 'img', 'bankid', 'bankname', 'isFixed')})
    state.update(txnType='ADD', fundClass=data.get('FUNDCLASS'))
    state.update({k: fpx[k] for k in FPX_FIELDS})
    return state


def open_confirmation(driver, state, account):
    """React Router v5 browser-history bridge, in the same authenticated tab."""
    if not isinstance(state, dict) or not state.get('FPX_URL') or not state.get('fpx_checkSum'):
        raise HandoffError('Missing confirmation state; manual review required.')
    result = driver.execute_script("""
        if (location.origin !== arguments[0] ||
            sessionStorage.getItem('isLoggedIn') !== 'true' ||
            sessionStorage.getItem('UHID') !== arguments[2]) return false;
        const entry = {key: Math.random().toString(36).slice(2, 8), state: arguments[1]};
        history.pushState(entry, '', '/paymentAddInv');
        window.dispatchEvent(new PopStateEvent('popstate', {state: entry}));
        return true;
    """, PORTAL, state, str(account))
    if result is not True:
        raise HandoffError('Session expired or account changed; manual review required.')

    def rendered(d):
        return d.execute_script("""
            if (location.pathname !== '/paymentAddInv') return false;
            const visible = e => e.getClientRects().length > 0;
            const checks = [...document.querySelectorAll('input[type=checkbox][value=agree]')].filter(visible);
            const payment = [...document.querySelectorAll('button')].filter(e =>
                visible(e) && /payment|pembayaran/i.test(e.innerText));
            return checks.length === 1 && !checks[0].checked &&
                payment.length === 1 && payment[0].disabled;
        """) is True
    try:
        WebDriverWait(driver, 15).until(rendered)
    except Exception:
        raise HandoffError('Confirmation rendering unverified; do not resubmit. Review browser manually.') from None


def import_http_session(driver, client):
    """Install an already-verified HTTP session into fresh Chrome; no UI login."""
    client._sync()
    driver.execute_cdp_cmd('Network.enable', {})
    driver.execute_cdp_cmd('Network.setCookies', {'cookies': client.browser_cookies()})
    # Install before React/router bootstraps, not after a redirect to the login page.
    source = """
        if (location.origin === %s) {
            const state = %s;
            for (const [key, value] of Object.entries(state)) sessionStorage.setItem(key, value);
        }
    """
    import json
    source = source % (json.dumps(PORTAL), json.dumps(client.storage))
    identifier = driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': source})
    try:
        driver.get(PORTAL + '/portfolio')
    finally:
        # Do not reinstate login flags on later navigation after logout.
        if isinstance(identifier, dict) and identifier.get('identifier'):
            driver.execute_cdp_cmd('Page.removeScriptToEvaluateOnNewDocument', identifier)
    driver.set_script_timeout(20)
    result = driver.execute_async_script("""
        const account = arguments[0], done = arguments[arguments.length - 1];
        if (location.origin !== 'https://www.myasnb.com.my' ||
            sessionStorage.getItem('UHID') !== account) { done(false); return; }
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        fetch('https://myasnb-api-v4.myasnb.com.my/v2/account/' + encodeURIComponent(account),
            {credentials:'include', headers:{Accept:'application/json'}, signal:controller.signal})
            .then(async response => {
                if (response.status !== 200) {done(false); return;}
                const body = await response.json();
                done(String(body.data?.UNITHOLDERID) === account);
            }).catch(() => done(false)).finally(() => clearTimeout(timer));
    """, str(client.account))
    if result is not True:
        raise HandoffError('Transferred browser session could not be verified; do not resubmit.')
