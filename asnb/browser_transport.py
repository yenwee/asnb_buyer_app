"""Subscription HTTP transport in the portal's authenticated Chrome context."""

from .api import APIReadError, ORIGIN
from selenium.webdriver.support.ui import WebDriverWait


PORTAL_READY_SCRIPT = r"""
const account = arguments[0];
if (location.origin !== 'https://www.myasnb.com.my' ||
    location.pathname !== '/portfolio' || document.readyState !== 'complete' ||
    sessionStorage.getItem('isLoggedIn') !== 'true' ||
    sessionStorage.getItem('UHID') !== account) return false;
// The synthetic account verification can finish before React initializes.
// Require the real portfolio bootstrap read to finish, not just a valid cookie.
const expected = 'https://myasnb-api-v4.myasnb.com.my/v2/portfolio/' + encodeURIComponent(account);
return performance.getEntriesByType('resource').some(entry =>
    entry.name === expected && entry.responseEnd > 0 && entry.responseStatus === 200);
"""


def wait_for_portal_ready(driver, account, timeout=45):
    """Read-only readiness barrier, before the submission latch or any POST."""
    try:
        WebDriverWait(driver, timeout, poll_frequency=.25).until(
            lambda current: current.execute_script(PORTAL_READY_SCRIPT, account) is True)
    except Exception:
        raise APIReadError('Portal initialization did not finish; no purchase submitted') from None


SUBSCRIPTION_SCRIPT = r"""
const account = arguments[0], fund = arguments[1], payload = arguments[2];
const timeout = arguments[3], done = arguments[arguments.length - 1];
if (location.origin !== 'https://www.myasnb.com.my' ||
    location.pathname !== '/portfolio' ||
    sessionStorage.getItem('isLoggedIn') !== 'true' ||
    sessionStorage.getItem('UHID') !== account ||
    !/^[A-Za-z0-9_-]{1,128}$/.test(account) ||
    !/^[A-Za-z0-9_-]{1,128}$/.test(fund)) {
    done({error: 'context_changed'}); return;
}
sessionStorage.setItem('TransactionType', 'ADD');
sessionStorage.setItem('FUNDID', fund);
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), timeout * 1000);
let headersMs;
const started = performance.now();
(async () => {
    try {
        const response = await fetch(
            'https://myasnb-api-v4.myasnb.com.my/v2/subscription/provisional/' +
            encodeURIComponent(account) + '/' + encodeURIComponent(fund), {
                method: 'POST', credentials: 'include', redirect: 'error',
                headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
                body: JSON.stringify(payload), signal: controller.signal
            });
        headersMs = Math.round(performance.now() - started);
        const reader = response.body.getReader();
        const chunks = [];
        let size = 0;
        for (;;) {
            const {value, done: ended} = await reader.read();
            if (ended) break;
            size += value.length;
            if (size > 1048576) {
                await reader.cancel();
                done({error: 'oversized_response'}); return;
            }
            chunks.push(value);
        }
        const bytes = new Uint8Array(size);
        let offset = 0;
        for (const chunk of chunks) {bytes.set(chunk, offset); offset += chunk.length;}
        const headers = {};
        for (const key of ['date', 'server', 'content-type']) {
            const value = response.headers.get(key);
            if (value !== null) headers[key] = value;
        }
        done({status: response.status, headers, text: new TextDecoder().decode(bytes), headersMs});
    } catch (_) {
        done({error: 'uncertain'});
    } finally {
        clearTimeout(timer);
    }
})();
"""


class BrowserResponse:
    def __init__(self, result):
        self.status_code = result['status']
        self.headers = result['headers']
        self.raw = result['text'].encode('utf-8')
        self.headers_ms = result['headersMs']

    def iter_content(self, size):
        yield self.raw

    def close(self):
        pass


def subscription_post(driver, account, fund, payload, timeout):
    """Exactly one fetch; failed acknowledgement is never retried here."""
    try:
        driver.set_script_timeout(timeout + 10)
        result = driver.execute_async_script(SUBSCRIPTION_SCRIPT, account, fund, payload, timeout)
        if (not isinstance(result, dict) or result.get('error') or
                not isinstance(result.get('status'), int) or
                not isinstance(result.get('headers'), dict) or
                not isinstance(result.get('headersMs'), (int, float)) or
                not isinstance(result.get('text'), str) or
                len(result['text'].encode('utf-8')) > 1024 * 1024):
            raise APIReadError('Chrome subscription acknowledgement unavailable; no transport retry')
        return BrowserResponse(result)
    except APIReadError:
        raise
    except Exception:
        raise APIReadError('Chrome subscription outcome unavailable; no transport retry') from None


def sync_cookies(driver, client):
    """Keep subsequent HTTP reads/logout aligned with Chrome cookie rotation."""
    result = driver.execute_cdp_cmd('Network.getCookies', {'urls': [ORIGIN + '/v2/settings']})
    cookies = result.get('cookies') if isinstance(result, dict) else None
    if not isinstance(cookies, list):
        raise APIReadError('Chrome session synchronization unavailable')
    # Only replace the API's applicable cookie scope; retain portal bootstrap cookies.
    for cookie in list(client.session.cookies):
        if cookie.domain.lstrip('.') in ('myasnb.com.my', 'myasnb-api-v4.myasnb.com.my'):
            client.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
    for cookie in cookies:
        if cookie.get('domain', '').lstrip('.') not in ('myasnb.com.my', 'myasnb-api-v4.myasnb.com.my'):
            continue
        client.session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'],
                                   path=cookie.get('path', '/'), secure=cookie.get('secure', True))
