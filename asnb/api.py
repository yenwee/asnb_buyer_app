"""Allowlisted, read-only API adapter using this browser's session in memory.

No login, subscription, payment, GraphQL, or arbitrary-URL methods are exposed.
API failure falls back to the normal UI; explicit session expiry/throttling do not.
"""

from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import json
import re
import time
from urllib.parse import urlsplit

import requests

from .purchase_state import block_retry_seconds

ORIGIN = 'https://myasnb-api-v4.myasnb.com.my'
_ACCOUNT = r'[A-Za-z0-9_-]{1,128}'
_READ_PATH = re.compile(r'(?:/maintenance|/v2/(?:settings|funds|subscription/fpxbanks)|'
                        r'/v2/(?:account|portfolio|transactions|getfundbusinesshour)/' + _ACCOUNT + r')\Z')


class APIReadError(Exception):
    """Static diagnostic only: never include request URLs, cookies or response data."""


class APISessionExpired(APIReadError):
    pass


class APIThrottled(APIReadError):
    def __init__(self, seconds=300):
        super().__init__('API requests throttled')
        self.seconds = seconds


def _name(value):
    return re.sub(r'\s*\(class\s+a\)$', '', ' '.join(str(value).lower().split())).strip()


def _money(value):
    try:
        result = Decimal(str(value).replace(',', ''))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


class PortalAPI:
    def __init__(self, driver):
        self.driver = driver
        self.session = requests.Session()
        self.cache = OrderedDict()
        self.account = None
        self.synced_at = None
        self.pause_until = 0
        self.throttle_until = 0
        self.last_portfolio_ok = None
        self.last_states = {}
        self.metrics = {'requests': 0, 'decoded_response_bytes': 0, 'cache_hits': 0, 'errors': 0}

    def reset(self):
        self.cache.clear()
        self.session.cookies.clear()
        self.account = None
        self.synced_at = None
        self.last_portfolio_ok = None
        self.last_states = {}
        # Preserve a server-directed transport cooldown across re-login.

    def close(self):
        self.reset()
        self.session.close()
        self.driver = None

    def _sync(self):
        if self.synced_at is not None and time.monotonic() - self.synced_at < 60:
            return
        try:
            meta = self.driver.execute_script("""
                return {url: performance.getEntriesByType('resource').map(e=>e.name).reverse().find(s=>{
                    try { const u=new URL(s);return u.origin===arguments[0] &&
                        /^\\/v2\\/portfolio\\/[A-Za-z0-9_-]{1,128}$/.test(u.pathname); }
                    catch (_) {return false;}
                }), account:sessionStorage.getItem('UHID'), ua:navigator.userAgent};
            """, ORIGIN)
            if not isinstance(meta, dict) or not isinstance(meta.get('url'), str):
                raise APIReadError('API session discovery unavailable')
            url = urlsplit(meta['url'])
            if url.scheme != 'https' or url.netloc != urlsplit(ORIGIN).netloc or url.query:
                raise APIReadError('Unexpected API session route')
            match = re.fullmatch(r'/v2/portfolio/(' + _ACCOUNT + ')', url.path)
            if not match or match[1] != meta.get('account') or not isinstance(meta.get('ua'), str):
                raise APIReadError('API session discovery unavailable')
            account = match[1]
            if self.account and account != self.account:
                raise APIReadError('API account changed; fresh browser required')
            cookies = self.driver.execute_cdp_cmd('Network.getCookies', {'urls': [ORIGIN + '/v2/settings']})
            if not isinstance(cookies, dict) or not isinstance(cookies.get('cookies'), list):
                raise APIReadError('API cookies unavailable')
            self.session.cookies.clear()
            for cookie in cookies['cookies']:
                if cookie.get('domain', '').lstrip('.') not in ('myasnb.com.my', 'myasnb-api-v4.myasnb.com.my'):
                    continue
                self.session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'],
                                         path=cookie.get('path', '/'), secure=cookie.get('secure', True))
            self.session.headers.update({'User-Agent': meta['ua'], 'Accept': 'application/json',
                                         'Origin': 'https://www.myasnb.com.my',
                                         'Referer': 'https://www.myasnb.com.my/'})
            self.account = account
            self.synced_at = time.monotonic()
        except APIReadError:
            raise
        except Exception:
            raise APIReadError('API session discovery unavailable') from None

    def _get(self, path, ttl=0):
        if not _READ_PATH.fullmatch(path):
            raise APIReadError('API route not allowlisted')
        now = time.monotonic()
        if now < self.throttle_until:
            raise APIThrottled(max(1, int(self.throttle_until - now + 0.999)))
        if path in self.cache and now - self.cache[path][0] < ttl:
            self.metrics['cache_hits'] += 1
            return self.cache[path][1]
        if now < self.pause_until:
            raise APIReadError('API temporarily unavailable')
        self._sync()
        account_path = re.fullmatch(r'/v2/(?:account|portfolio|transactions|getfundbusinesshour)/(' + _ACCOUNT + ')', path)
        if account_path and account_path[1] != self.account:
            raise APIReadError('API account scope mismatch')
        try:
            self.metrics['requests'] += 1
            response = self.session.get(ORIGIN + path, timeout=(3, 5), allow_redirects=False, stream=True)
            try:
                if response.status_code == 401:
                    self.reset()
                    raise APISessionExpired('API session expired')
                if response.status_code == 429:
                    value = str(response.headers.get('Retry-After', ''))
                    seconds = max(1, min(86400, int(value))) if value.isdigit() else 300
                    self.throttle_until = time.monotonic() + seconds
                    raise APIThrottled(seconds)
                if response.status_code != 200:
                    raise APIReadError('API read unavailable')
                chunks, size = [], 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise APIReadError('API response exceeded memory bound')
                    chunks.append(chunk)
                self.metrics['decoded_response_bytes'] += size
                result = json.loads(b''.join(chunks))
                errors = result.get('errors') if isinstance(result, dict) else None
                if isinstance(errors, dict):
                    errors = {k: v for k, v in errors.items() if k != 'ReferenceId' and v is not None}
                if not isinstance(result, dict) or errors or result.get('error'):
                    raise APIReadError('API response unavailable')
            finally:
                response.close()
        except (APISessionExpired, APIThrottled):
            raise
        except Exception:
            self.metrics['errors'] += 1
            self.pause_until = time.monotonic() + 30
            raise APIReadError('API read unavailable; normal UI fallback') from None
        self.cache[path] = (time.monotonic(), result)
        self.cache.move_to_end(path)
        while len(self.cache) > 8:
            self.cache.popitem(last=False)
        return result

    def portfolio(self, force=False):
        self._sync()
        result = self._get('/v2/portfolio/' + self.account, ttl=0 if force else 30)
        if not isinstance(result.get('data'), list) or len(result['data']) > 500:
            raise APIReadError('Portfolio schema changed')
        self.last_portfolio_ok = time.monotonic()
        return result['data']

    def _fund(self, rows, variants):
        if not isinstance(rows, list):
            return None
        names = {_name(v) for v in variants}
        matches = [row for row in rows if isinstance(row, dict) and _name(row.get('NAME', '')) in names
                   and str(row.get('FUNDCLASS', '')).upper().replace('_', '').replace(' ', '') not in ('B', 'CLASSB')]
        return matches[0] if len(matches) == 1 else None

    def preflight(self, funds, bank):
        rows = self.portfolio(force=True)
        states = {}
        for name, variants in funds.items():
            row = self._fund(rows, variants)
            state = {'state': 'UNKNOWN'}
            if row:
                reason = str(row.get('IS_ACTIVE_REASON', ''))
                flag = row.get('IS_ACTIVE')
                if flag == 1:
                    state = {'state': 'ELIGIBLE', 'fund_id': str(row.get('FUNDID', ''))}
                elif flag in (0, 4, 5, 6, 7, 8):
                    blocked = bool(re.search('blocked due|too many retr|try again in|1001', reason, re.I))
                    state = {'state': 'BLOCKED' if blocked else 'INACTIVE',
                             'seconds': block_retry_seconds(reason)}
            states[name] = state
        self.last_states = states
        # Do not fetch optional metadata when every configured fund is already unavailable.
        if not any(s['state'] in ('ELIGIBLE', 'UNKNOWN') for s in states.values()):
            return states
        try:
            maintenance = self._get('/maintenance', ttl=60)
            settings = self._get('/v2/settings', ttl=60).get('data', {})
            hours = self._get('/v2/getfundbusinesshour/' + self.account, ttl=60).get('data', [])
            if not isinstance(hours, list):
                raise APIReadError('Operating hours schema changed')
            closed = (str(maintenance.get('MAINTENANCE_ACTIVE_PORTAL', '')).lower() in ('true', '1', 'yes', 'y')
                      or (isinstance(settings, dict) and settings.get('subscription_available') is False)
                      or any(isinstance(h, dict) and _name(h.get('desc')) == 'fixed price'
                             and str(h.get('status', '')).lower() == 'closed' for h in hours))
            banks = self._get('/v2/subscription/fpxbanks', ttl=3600).get('data', [])
            if not isinstance(banks, list):
                raise APIReadError('Bank metadata schema changed')
            matching = [b for b in banks if isinstance(b, dict) and any(
                _name(b.get(k, '')) == _name(bank) for k in ('FPX_BANK_NAME', 'FULL_NAME'))]
            bank_closed = len(matching) == 1 and matching[0].get('IS_ACTIVE') == 0
            if bank_closed:
                # Recheck an unavailable bank after the 60-second hold, not an hour later.
                self.cache.pop('/v2/subscription/fpxbanks', None)
            if closed or bank_closed:
                for name in states:
                    if states[name]['state'] in ('ELIGIBLE', 'UNKNOWN'):
                        states[name] = {'state': 'CHANNEL_CLOSED' if closed else 'BANK_UNAVAILABLE', 'seconds': 60}
        except (APISessionExpired, APIThrottled):
            raise
        except APIReadError:
            pass  # Valid eligibility remains useful; unavailable metadata uses normal UI checks.
        return states

    def resolve_fund(self, variants):
        row = self._fund(self.portfolio(), variants)
        if row:
            return str(row.get('FUNDID', '')) or None
        row = self._fund(self._get('/v2/funds', ttl=3600).get('data', []), variants)
        return str(row.get('ID', '')) if row else None

    def history(self):
        self._sync()
        body = self._get('/v2/transactions/' + self.account)
        if not isinstance(body.get('data'), list):
            raise APIReadError('Transaction schema changed')
        rows, visited = [], 0
        def walk(value, depth=0):
            nonlocal visited
            visited += 1
            if visited > 5000 or depth > 8:
                raise APIReadError('Transaction history exceeded memory bound')
            if isinstance(value, dict):
                if 'TRANSACTION_STATUS' in value and ('TRANSACTION_REF_NO' in value or 'ID' in value):
                    rows.append(value)
                else:
                    for child in value.values():
                        walk(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    walk(child, depth + 1)
        walk(body['data'])
        if body['data'] and not rows:
            raise APIReadError('Transaction schema changed')
        return rows

    @staticmethod
    def _reference(row):
        value = row.get('TRANSACTION_REF_NO') or row.get('ID')
        return str(value) if value is not None else None

    def snapshot(self, variants, amount):
        fund_id = self.resolve_fund(variants)
        if not fund_id or _money(amount) is None:
            raise APIReadError('Submission context unavailable')
        return {'references': {self._reference(r) for r in self.history()},
                'fund_id': fund_id, 'amount': _money(amount)}

    def reconcile(self, snapshot):
        if not isinstance(snapshot, dict):
            return 'unavailable; manual review required'
        matches = [r for r in self.history() if self._reference(r) not in snapshot['references']
                   and str(r.get('FUND_ID', '')) == snapshot['fund_id']
                   and _money(r.get('INV_AMOUNT')) == snapshot['amount']]
        if not matches:
            return 'no new matching record yet; absence is not proof of failure'
        if len(matches) != 1:
            return 'multiple new matching records; manual review required'
        status = str(matches[0].get('TRANSACTION_STATUS', '')).upper()
        if status not in ('SUCCESS', 'SUCCESSFUL', 'FAILED', 'UNSUCCESSFUL', 'PENDING', 'PROCESSING',
                          'COMPLETED', 'CANCELLED', 'REJECTED'):
            status = 'UNKNOWN'
        return f'one new matching record ({status}); verify manually before resuming'


def get_api(driver):
    client = getattr(driver, '_asnb_api', None)
    return client if isinstance(client, PortalAPI) else None
