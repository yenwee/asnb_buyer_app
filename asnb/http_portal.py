"""HTTP portal session with optional Chrome subscription transport; bounded writes."""

import base64
import copy
import hashlib
import hmac
import json
import re
import time
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

import requests

from .api import PortalAPI, ORIGIN, APIReadError, APISessionExpired, _name
from .handoff import HandoffError, PORTAL, confirmation_state, money, response_errors
from .purchase_state import block_retry_seconds


class LoginError(APIReadError):
    pass


def _auth_errors(body):
    errors = body.get('errors') if isinstance(body, dict) else None
    if isinstance(errors, dict):
        return {k: v for k, v in errors.items() if k != 'ReferenceId' and v is not None}
    return errors


class HTTPPortal(PortalAPI):
    def __init__(self, purchase_timeout=60):
        super().__init__(None)
        if not isinstance(purchase_timeout, int) or not 20 <= purchase_timeout <= 180:
            raise ValueError('API purchase timeout must be between 20 and 180 seconds')
        self.purchase_timeout = purchase_timeout
        self.authenticated = False
        self.storage = {}
        self.token = None
        self.submission_locked = False
        self.confirmation = None
        self.block_seconds = 300
        self.last_write = {}
        self.portfolio_rows = []
        self.portfolio_value = None
        self.session.headers.update({'Accept': 'application/json', 'Origin': PORTAL, 'Referer': PORTAL + '/'})

    def reset(self):
        super().reset()
        self.authenticated = False
        self.storage = {}
        self.token = None
        self.portfolio_rows = []
        self.portfolio_value = None
        # Never release an accepted/uncertain submission as a side effect of reset.

    def _sync(self):
        if not self.authenticated or not self.account:
            raise APISessionExpired('HTTP session unavailable')

    @staticmethod
    def _body(response, limit=1024 * 1024):
        try:
            parts, size = [], 0
            for chunk in response.iter_content(16384):
                size += len(chunk)
                if size > limit:
                    raise APIReadError('HTTP response exceeded memory bound')
                parts.append(chunk)
            return b''.join(parts)
        finally:
            response.close()

    def _post(self, path, payload):
        permitted = path in ('/login/securephrase', '/v2/login', '/v2/logout')
        if self.account:
            permitted |= bool(re.fullmatch(r'/v2/subscription/provisional/' + re.escape(self.account) + r'/[A-Za-z0-9_-]{1,128}', path))
        if not permitted:
            raise APIReadError('HTTP write route not allowlisted')
        self.last_write = {'stage': 'subscription' if '/subscription/' in path else 'authentication'}
        started = time.monotonic()
        try:
            # Axios uses JSON.stringify: compact UTF-8 with a fixed-length body.
            # Let requests calculate Content-Length from bytes, never characters.
            if path == '/v2/logout':
                arguments = {'data': ''}
            else:
                encoded = json.dumps(payload, separators=(',', ':'), ensure_ascii=False,
                                     allow_nan=False).encode('utf-8')
                arguments = {'data': encoded, 'headers': {'Content-Type': 'application/json'}}
                self.last_write['request_bytes'] = len(encoded)
            timeout = self.purchase_timeout if '/subscription/provisional/' in path else 20
            if '/subscription/' in path and self.driver is not None:
                from .browser_transport import subscription_post, sync_cookies
                self.last_write['transport'] = 'chrome-fetch'
                response = subscription_post(self.driver, self.account, path.rsplit('/', 1)[1],
                                             payload, timeout)
                # A cookie-copy failure must not hide an accepted response.
                try:
                    sync_cookies(self.driver, self)
                except Exception:
                    self.last_write['cookie_sync'] = 'unavailable'
            else:
                self.last_write['transport'] = 'python-http'
                response = self.session.post(ORIGIN + path, **arguments, timeout=(5, timeout),
                                             allow_redirects=False, stream=True)
            status = response.status_code
            self.last_write['http'] = status
            self.last_write['response_headers_ms'] = getattr(response, 'headers_ms',
                round((time.monotonic() - started) * 1000))
            # Record categories only, never arbitrary header values or trace IDs.
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            server = headers.get('server', '').lower()
            self.last_write['edge'] = server if server in ('esa', 'cloudflare', 'nginx', 'openresty') else 'other'
            self.last_write['cf_ray_present'] = 'cf-ray' in headers
            media_type = headers.get('content-type', '').split(';')[0].strip().lower()
            self.last_write['response_type'] = media_type if media_type in (
                'application/json', 'text/html', 'text/plain') else 'other'
            date = headers.get('date')
            raw = self._body(response)
            self.last_write['response_bytes'] = len(raw)
            if status != 200:
                if status == 524:
                    raise APIReadError('HTTP 524 received; no subscription acknowledgement to parse')
                raise APIReadError('HTTP write was not acknowledged successfully')
            if path == '/v2/logout':
                return {}, date
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise APIReadError('HTTP response schema changed')
            data = body.get('data')
            if isinstance(data, dict):
                status_value = data.get('TRANSACTIONSTATUS')
                if status_value in ('Successful', 'Reject', 'Rejected', 'Pending', 'Processing'):
                    self.last_write['transaction_status'] = status_value
                code = str(data.get('REJECTCODE', '')).strip()
                if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code):
                    self.last_write['reject_code'] = code
            errors = response_errors(body)
            if isinstance(errors, dict):
                self.last_write['error_codes'] = [key for key in errors if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', str(key))]
            return body, date
        except APIReadError as error:
            self.last_write['failure'] = str(error)
            raise
        except Exception as error:
            self.last_write['exception'] = type(error).__name__
            raise APIReadError('HTTP write outcome unavailable; no transport retry') from None
        finally:
            self.last_write['elapsed_ms'] = round((time.monotonic() - started) * 1000)

    def _login_material(self):
        """Read the current public frontend's normal login serialization material."""
        try:
            page = self.session.get(PORTAL + '/login', timeout=(5, 20), allow_redirects=False, stream=True)
            status = page.status_code
            html = self._body(page).decode()
            if status != 200:
                raise LoginError('Login bootstrap unavailable; no credential retry')
            match = re.search(r'(?:src|href)=["\']([^"\']*index-[^"\']+\.js)["\']', html)
            url = urljoin(PORTAL, match[1]) if match else ''
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.netloc != 'www.myasnb.com.my' or not parsed.path.startswith('/assets/') or parsed.query:
                raise LoginError('Frontend login contract changed; manual review required')
            response = self.session.get(url, timeout=(5, 30), allow_redirects=False, stream=True)
            status = response.status_code
            source = self._body(response, 10 * 1024 * 1024).decode()
            key = re.search(r'Hs=sl\.connectionWV,to="([^"]+)"', source)
            if status != 200 or not key or 'setProtectedHeader({alg:"HS512"})' not in source:
                raise LoginError('Frontend login contract changed; manual review required')
            return key[1].encode()
        except LoginError:
            raise
        except Exception:
            raise LoginError('Login bootstrap unavailable; no credential retry') from None

    def login(self, username, password):
        if self.submission_locked:
            raise LoginError('An accepted or uncertain order requires review before re-login')
        self.reset()
        key = self._login_material()
        phrase, server_date = self._post('/login/securephrase', {'username': username})
        data = phrase.get('data')
        if _auth_errors(phrase) or not isinstance(data, dict) or not data.get('security_phrase'):
            raise LoginError('Security-phrase step requires review; no login attempted')
        now = int(time.time())
        if server_date:
            try:
                now = int(parsedate_to_datetime(server_date).timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
        claims = dict(iat=now, exp=now + 60, aud='www.myasnb.com.my', sub=username,
                      password=password, applicationID='MYASNB', deviceId='')
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
        message = encode({'alg': 'HS512'}) + b'.' + encode(claims)
        signature = base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha512).digest()).rstrip(b'=')
        body, _ = self._post('/v2/login', {'jwt': (message + b'.' + signature).decode()})
        if not body.get('token') or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(body.get('uhid', ''))):
            raise LoginError('Login rejected or requires a challenge/session review; no credential retry')
        self.authenticated, self.account, self.token = True, str(body['uhid']), body['token']
        person = self._get('/v2/account/' + self.account).get('data')
        if not isinstance(person, dict) or str(person.get('UNITHOLDERID')) != self.account:
            raise LoginError('Authenticated account verification failed')
        self.portfolio_rows = self.portfolio(force=True)
        cached = self.cache.get('/v2/portfolio/' + self.account, (0, {}))[1]
        meta = cached.get('meta', {})
        self.portfolio_value = meta.get('GRANDTOTALUHHOLDINGS') if isinstance(meta, dict) else None
        self.storage = {'isLoggedIn': 'true', 'UHID': self.account, 'username': username,
                        'FIRSTNAME': str(person.get('FIRSTNAME', '')), 'EMAIL': str(person.get('EMAIL', '')),
                        'USERCATEGORY': str(person.get('INVESTORFLAG', ''))}
        return True

    def balance_lines(self, selection=None):
        """Show portal holdings, not a bank cash balance or unit availability."""
        def number(value):
            try:
                result = Decimal(str(value).replace(',', ''))
                return f'{result:,.2f}' if result.is_finite() else 'unavailable'
            except (InvalidOperation, ValueError):
                return 'unavailable'
        rows = self.portfolio_rows
        total = number(self.portfolio_value)
        lines = [f'Portfolio: {len(rows)} funds | current holdings value: RM {total}']
        if isinstance(selection, dict):
            selected = []
            for configured, aliases in selection.items():
                row = self._fund(rows, aliases)
                if row is not None:
                    selected.append(row)
            rows = selected
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = ' '.join(str(row.get('SHORT_NAME') or row.get('NAME') or 'Fund').split())[:100]
            lines.append(f"  {name}: RM {number(row.get('UHHOLDINGS'))} | {number(row.get('TOTALUNITS'))} units")
        return lines

    def purchase_diagnostic(self):
        return json.dumps(self.last_write, separators=(',', ':'))

    def context(self, fund_name, amount, bank_name, fund_aliases=None):
        fund = self._fund(self.portfolio(force=True), fund_aliases or [fund_name])
        if not fund or fund.get('IS_ACTIVE') != 1 or fund.get('IS_FIXED') != 1 or fund.get('FUNDCLASS') != 'A':
            raise APIReadError('Eligible fixed-price Class A fund unavailable')
        banks = self._get('/v2/subscription/fpxbanks', ttl=60).get('data')
        matches = [b for b in banks if isinstance(b, dict) and _name(b.get('FPX_BANK_NAME')) == _name(bank_name)] if isinstance(banks, list) else []
        if len(matches) != 1 or matches[0].get('IS_ACTIVE') == 0 or not matches[0].get('FPX_BANK_CODE'):
            raise APIReadError('Configured FPX bank unavailable or ambiguous')
        if not fund.get('FUNDID') or not fund.get('SHORT_NAME'):
            raise APIReadError('Fund metadata unavailable')
        return dict(account=self.account, fund_id=str(fund['FUNDID']), fundshortname=fund['SHORT_NAME'],
                    img=fund.get('FUND_LOGO'), isFixed=1, bankid=str(matches[0]['FPX_BANK_CODE']),
                    bankname=matches[0]['FPX_BANK_NAME'], amount=str(money(amount)))

    def purchase(self, context, referral=''):
        self._sync()
        if self.submission_locked or context.get('account') != self.account:
            raise HandoffError('Submission locked or account changed; manual review required')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', context.get('fund_id', '')):
            raise HandoffError('Invalid fund context; no submission')
        payload = dict(amount=context['amount'], fpxBankId=context['bankid'], TXN_CODE='101',
                       CRITERIACONFIRM=True, PROSPECTUSAGREE=True, riskSrcOfFund='', riskSrcOfWealth='',
                       riskOtherSrcOfFund='', riskOtherSrcOfWealth='', referral_code=referral)
        self.submission_locked = True
        try:
            body, _ = self._post('/v2/subscription/provisional/' + self.account + '/' + context['fund_id'], payload)
            data = body.get('data')
            if (isinstance(data, dict) and not response_errors(body) and not body.get('error')
                    and not (isinstance(body.get('meta'), dict) and body['meta'].get('Error'))
                    and data.get('TRANSACTIONSTATUS') in ('Reject', 'Rejected') and not data.get('FPX_DATA')):
                code = str(data.get('REJECTCODE', '')).strip()
                if code in ('068', '1001'):
                    self.submission_locked = False
                    self.block_seconds = block_retry_seconds(str(data.get('REJECTREASON', '')))
                    return 'INSUFFICIENT' if code == '068' else 'BLOCKED'
            self.confirmation = confirmation_state(body, context)
            return 'SUCCESS'
        except HandoffError:
            raise
        except Exception:
            raise HandoffError('Purchase transport/response unavailable; do not resubmit. Review required.') from None

    def logout(self, verify=False):
        """Optionally replay the pre-logout cookie jar for one read-only check."""
        if not self.authenticated:
            return {'logoutHTTP': None, 'oldSessionRejected': None}
        account, old_cookies = self.account, copy.deepcopy(self.session.cookies)
        result = {'logoutHTTP': None, 'oldSessionRejected': None}
        try:
            self._post('/v2/logout', '')
            result['logoutHTTP'] = 200
            if verify:
                with requests.Session() as old:
                    old.headers.update(self.session.headers)
                    old.cookies.update(old_cookies)
                    response = old.get(ORIGIN + '/v2/account/' + account, timeout=(5, 10),
                                       allow_redirects=False, stream=True)
                    result['postLogoutHTTP'] = response.status_code
                    raw = self._body(response)
                    result['oldSessionRejected'] = result['postLogoutHTTP'] in (401, 403)
                    if result['postLogoutHTTP'] == 200:
                        try:
                            body = json.loads(raw)
                            result['oldSessionStillAuthenticated'] = str(body.get('data', {}).get('UNITHOLDERID')) == account
                        except (ValueError, AttributeError):
                            result['oldSessionStillAuthenticated'] = None
            return result
        finally:
            self.reset()

    def browser_cookies(self):
        cookies = []
        for c in self.session.cookies:
            if c.domain.lstrip('.') not in ('myasnb.com.my', 'www.myasnb.com.my', 'myasnb-api-v4.myasnb.com.my'):
                continue
            item = dict(name=c.name, value=c.value, domain=c.domain, path=c.path, secure=c.secure,
                        httpOnly=any(k.lower() == 'httponly' for k in c._rest))
            if c.expires:
                item['expires'] = c.expires
            same = next((v for k, v in c._rest.items() if k.lower() == 'samesite'), None)
            if same and same.capitalize() in ('Lax', 'Strict', 'None'):
                item['sameSite'] = same.capitalize()
            cookies.append(item)
        return cookies
