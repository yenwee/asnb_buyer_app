"""HTTP-first ordering with temporary Chrome session initialization."""

import itertools
import math
import random
import time
from selenium.common.exceptions import WebDriverException

from .api import APIReadError, APISessionExpired, APIThrottled
from .handoff import HandoffError, import_http_session, open_confirmation
from .http_portal import HTTPPortal, LoginBootstrapUnavailable
from .browser_transport import wait_for_portal_ready, sync_cookies
from .driver import DriverSetupError


def _fund_label(fund):
    return fund.replace('Amanah Saham Malaysia', 'ASM')


def _duration(seconds):
    minutes, seconds = divmod(math.ceil(seconds), 60)
    return f'{minutes}:{seconds:02d}'


def hold_status(fund, state, remaining):
    reasons = {
        'FUND_BLOCKED': 'portal reports fund blocked',
        'FUND_INACTIVE': 'fund inactive',
        'MAINTENANCE': 'portal maintenance',
        'SUBSCRIPTIONS_DISABLED': 'subscriptions disabled',
        'OUTSIDE_BUSINESS_HOURS': 'outside fund business hours',
        'BANK_INACTIVE': 'selected bank unavailable',
    }
    fallback = {'BLOCKED': 'FUND_BLOCKED', 'INACTIVE': 'FUND_INACTIVE', 'BANK_UNAVAILABLE': 'BANK_INACTIVE'}
    reason = reasons.get(state.get('reason'), reasons.get(fallback.get(state['state']), 'subscription channel closed'))
    return f"API eligibility: {_fund_label(fund)}: {state['state']} ({reason}); remaining {_duration(remaining)}."


def cooldown_status(funds, tracker):
    """Snapshot per-fund waits and wake no later than the earliest expiry."""
    remaining = {fund: tracker.remaining_cooldown(fund) for fund in funds}
    earliest = min(remaining, key=remaining.get)
    delay = min(60, max(.01, remaining[earliest]))

    counters = ' | '.join(f'{_fund_label(fund)}: {_duration(seconds)}' for fund, seconds in remaining.items())
    message = (f'Cooldowns: {counters}. Next eligible: {_fund_label(earliest)} in '
               f'{_duration(remaining[earliest])}; status update in {delay:.1f}s.')
    return delay, message


def run_http_runner(username, password, funds, amount, bank, referral, loops,
                    refresh_interval, email, new_browser, hooks, purchase_timeout=60,
                    subscription_transport='http'):
    if subscription_transport not in ('chrome', 'http'):
        raise ValueError('API subscription transport must be chrome or http')
    print = hooks.print  # Same flushed console + per-profile file log as the UI runner.
    client = HTTPPortal(purchase_timeout=purchase_timeout)
    browser = None
    snapshot = None
    http_session_bootstrapped = False
    last_hold_states = {}
    purchase_attempts = {fund: 0 for fund in funds}
    browser_failures = 0

    def stop_for_review(message):
        print(message)
        print('Automatic ordering stopped. No accepted/uncertain request will be replayed.')
        try:
            print('API transaction history:', client.reconcile(snapshot))
        except APIReadError:
            print('Transaction history unavailable; review your account manually.')
        # Do not allocate Chrome for an unknown purchase outcome or restart the
        # outer CLI loop after acknowledgement. A fresh order needs a new run.
        try:
            hooks.wait_for_submission_review(browser)
        finally:
            raise KeyboardInterrupt()

    def login():
        nonlocal http_session_bootstrapped
        http_session_bootstrapped = False
        last_hold_states.clear()
        print(f'HTTP login: subscription transport={subscription_transport}; no purchase-form navigation.')
        bootstrap_failures = 0
        while True:
            try:
                client.login(username, password)
                break
            except LoginBootstrapUnavailable as error:
                # This exception is restricted to public GETs before credentials.
                # A rejected/lost credential POST never enters this retry path.
                bootstrap_failures += 1
                delay = max(error.retry_after, min(60, 5 * 2 ** min(bootstrap_failures - 1, 4)))
                print(str(error))
                print(f'Public login bootstrap retry {bootstrap_failures} in {delay}s; ordering remains paused.')
                remaining = delay
                while remaining > 0:
                    step = min(60, remaining)
                    time.sleep(step)
                    remaining -= step
                    if remaining:
                        print(f'Public bootstrap cooldown: {remaining}s remaining; no requests sent.')
            except APIReadError as error:
                print(str(error))
                print('Authentication requires review. Stopping without credential retries.')
                raise KeyboardInterrupt() from None
        print('HTTP login verified by authenticated account and portfolio reads.')
        for line in client.balance_lines({f: hooks.fund_name_variants(f) for f in funds}):
            print(line)

    try:
        login()
        attempts = 0
        maximum = loops * len(funds) if loops > 0 else 0
        for fund in itertools.cycle(funds):
            if maximum and attempts >= maximum:
                break
            if hooks.fund_cooldowns.is_on_cooldown(fund):
                if all(hooks.fund_cooldowns.is_on_cooldown(f) for f in funds):
                    delay, message = cooldown_status(funds, hooks.fund_cooldowns)
                    print(message)
                    time.sleep(delay)
                continue
            attempts += 1
            hooks.stats.record_attempt(fund)
            hooks.maybe_send_summary_email(email)
            try:
                states = client.preflight({f: hooks.fund_name_variants(f) for f in funds}, bank)
                for name, state in states.items():
                    if state['state'] in ('BLOCKED', 'INACTIVE', 'CHANNEL_CLOSED', 'BANK_UNAVAILABLE'):
                        # A repeated portfolio flag is not a new rejection. Do not
                        # restart another fund's existing countdown on every read.
                        new_hold = not hooks.fund_cooldowns.is_on_cooldown(name)
                        if new_hold:
                            hooks.fund_cooldowns.mark_blocked(name, state['seconds'])
                        signature = (state['state'], state.get('reason'))
                        if new_hold or last_hold_states.get(name) != signature:
                            remaining = hooks.fund_cooldowns.remaining_cooldown(name)
                            print(hold_status(name, state, remaining))
                        last_hold_states[name] = signature
                    else:
                        last_hold_states.pop(name, None)
                if states.get(fund, {}).get('state') != 'ELIGIBLE':
                    if fund not in last_hold_states:
                        print(f'API eligibility: {fund}: {states.get(fund, {}).get("state", "UNKNOWN")}; holding for cooldown.')
                    if not hooks.fund_cooldowns.is_on_cooldown(fund):
                        hooks.fund_cooldowns.mark_blocked(fund, 30)
                    continue
                # Preflight already forces a fresh portfolio each fund cycle.
                try:
                    snapshot = client.snapshot(hooks.fund_name_variants(fund), amount)
                except APISessionExpired:
                    raise
                except APIReadError:
                    snapshot = None
                context = client.context(fund, amount, bank,
                                         fund_aliases=hooks.fund_name_variants(fund),
                                         reuse_preflight=True)
                if browser is None and (subscription_transport == 'chrome' or not http_session_bootstrapped):
                    print('Starting Chrome API transport and transferring the authenticated session.')
                    stage = 'Chrome startup'
                    try:
                        browser = new_browser()
                        stage = 'session transfer'
                        print('Chrome started; importing the HTTP session into the portfolio tab.')
                        import_http_session(browser, client)
                        stage = 'portal initialization'
                        print('Waiting for the portal application and portfolio initialization before submitting.')
                        wait_for_portal_ready(browser, client.account)
                        if subscription_transport == 'http':
                            sync_cookies(browser, client)
                            client.driver = None
                            http_session_bootstrapped = True
                        else:
                            client.driver = browser
                    except APISessionExpired:
                        raise
                    except (DriverSetupError, WebDriverException, APIReadError) as error:
                        # No subscription has been dispatched at this point. Retry
                        # initialization on a later turn with a fresh preflight.
                        browser_failures += 1
                        delay = min(60, 5 * 2 ** min(browser_failures - 1, 4))
                        print(f'{stage} unavailable ({type(error).__name__}); no purchase submitted. Retrying in {delay}s.')
                        if browser is not None:
                            try:
                                hooks.close_webdriver(browser)
                            except Exception:
                                print('Failed Chrome cleanup unavailable.')
                        browser = None
                        client.driver = None
                        time.sleep(delay)
                        continue
                    except Exception:
                        if browser is not None:
                            hooks.close_webdriver(browser)
                        browser = None
                        client.driver = None
                        raise
                    browser_failures = 0
                    print(f'Portal initialization complete; {subscription_transport} API transport ready.')
                    try:
                        browser.minimize_window()
                    except Exception:
                        print('Chrome could not minimize; API ordering continues in its portfolio tab.')
                purchase_attempts[fund] += 1
                print(f'API purchase: {fund}, attempt {purchase_attempts[fund]}; one attempt per turn; waiting up to {purchase_timeout}s for the response.')
                outcome = client.purchase(context, referral)
                print(f'API response: {outcome}; {client.purchase_diagnostic()}')
                if subscription_transport == 'http' and outcome in ('INSUFFICIENT', 'BLOCKED') and browser is not None:
                    # Only an explicit acknowledgement proves this initialized
                    # HTTP session is working. Keep cookies in memory afterward.
                    hooks.close_webdriver(browser)
                    browser = None
                    print('HTTP session acknowledged. Chrome closed; API ordering continues without a browser.')
                if outcome == 'INSUFFICIENT':
                    hooks.stats.record_insufficient()
                    delay = random.uniform(3, 8)
                    print(f'Insufficient units (068); rotating to the next eligible fund after {delay:.1f}s.')
                    time.sleep(delay)
                    continue
                if outcome == 'BLOCKED':
                    hooks.stats.record_block(fund)
                    hooks.fund_cooldowns.mark_blocked(fund, client.block_seconds)
                    print(f'Fund blocked (1001); cooldown {client.block_seconds}s. Rotating funds.')
                    continue
                if outcome != 'SUCCESS':
                    stop_for_review('Unrecognized purchase outcome.')
                # Success remains locked while browser construction/transfer/rendering runs.
                try:
                    print('Subscription accepted. Showing Chrome for manual agreement and FPX.')
                    if browser is None:
                        browser = new_browser()
                    if subscription_transport == 'http':
                        # Preserve any cookies rotated by the HTTP response.
                        import_http_session(browser, client)
                        wait_for_portal_ready(browser, client.account)
                    try:
                        browser.maximize_window()
                    except Exception:
                        pass
                    open_confirmation(browser, client.confirmation, client.account)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    stop_for_review('Accepted order; Chrome handoff failed. Do not place another order.')
                action = hooks.pause_for_payment(browser, fund, amount, email)
                if action != 'RESUME':
                    raise KeyboardInterrupt()
                # Resume explicitly follows the user's manual payment/review.
                hooks.close_webdriver(browser)
                browser = None
                client.driver = None
                # The user has explicitly reviewed/completed payment. Logout is
                # session cleanup, not another subscription acknowledgement.
                try:
                    client.logout()
                except APIReadError:
                    print('Post-payment logout not acknowledged; local session discarded. Starting fresh HTTP login.')
                    client.reset()
                client.submission_locked = False
                client.confirmation = None
                login()
                continue
            except APISessionExpired:
                if client.submission_locked:
                    stop_for_review('Session changed after dispatch; manual review required.')
                print('Read-only API session expired; refreshing HTTP login before further ordering.')
                client.logout()
                if browser is not None:
                    hooks.close_webdriver(browser)
                    browser = None
                    client.driver = None
                login()
                hooks.stats.record_refresh()
            except APIThrottled as error:
                for name in funds:
                    hooks.fund_cooldowns.mark_blocked(name, error.seconds)
            except HandoffError as error:
                print(f'Purchase diagnostic: {client.purchase_diagnostic()}')
                print(str(error))
                stop_for_review('Purchase or handoff outcome requires manual review; no automatic retry.')
            except APIReadError:
                if client.submission_locked:
                    stop_for_review('API failed after dispatch; no automatic retry.')
                print('Read-only API check unavailable; holding this fund for 30 seconds.')
                hooks.fund_cooldowns.mark_blocked(fund, 30)
        print(hooks.stats.summary())
    except KeyboardInterrupt:
        raise
    except Exception as error:
        if client.submission_locked:
            stop_for_review('Runner failed after dispatch; no accepted/uncertain order will be replayed.')
        print(f'HTTP runner stopped after an unexpected error ({type(error).__name__}); no credential retry.')
        raise KeyboardInterrupt() from None
    finally:
        if browser is not None:
            try:
                hooks.close_webdriver(browser)
            except Exception:
                print('Browser cleanup unavailable.')
        try:
            result = client.logout()
            if result['logoutHTTP'] is not None:
                print('HTTP logout acknowledged.')
        except Exception:
            print('HTTP logout could not be confirmed; local session discarded.')
        client.close()
