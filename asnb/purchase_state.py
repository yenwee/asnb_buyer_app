"""Poll visible purchase states together instead of waiting on each in turn."""

from selenium.webdriver.support.ui import WebDriverWait
from .network import trace_network, trace_outcome
from selenium.common.exceptions import WebDriverException
import re


def block_retry_seconds(text, default=300):
    """Honor a visible retry instruction; use five minutes if unspecified."""
    match = re.search(r'(?:try again in|cuba semula dalam)\s+(\d+)\s*'
                      r'(minutes?|seconds?|minit|saat)', text, re.I)
    if not match:
        return default
    seconds = int(match[1]) * (60 if match[2].lower().startswith('min') else 1)
    return seconds if 0 < seconds <= 86400 else default



def submit_and_observe(driver, button, timeout=20):
    """Click once. A command error may occur after dispatch; never blindly replay."""
    try:
        button.click()
    except WebDriverException:
        pass
    try:
        return wait_purchase_state(driver, timeout=timeout)
    except WebDriverException:
        trace_outcome(driver, 'submission', 'UNCERTAIN')
        return 'UNCERTAIN'


def wait_purchase_state(driver, navigation=False, timeout=15):
    script = """
        const visible = e => !!(e.getClientRects().length) &&
            getComputedStyle(e).visibility !== 'hidden' &&
            getComputedStyle(e).display !== 'none';
        const text = [...document.querySelectorAll('p, [role="alert"], div.fixed.inset-0')]
            .filter(visible).map(e => e.innerText).join(' ').toLowerCase();
        if (/1001|blocked due to|too many retr|too many request|try again in 5 minutes/.test(text))
            return 'BLOCKED';
        if (/068|insufficient units available/.test(text)) return 'INSUFFICIENT';
        if (arguments[0]) {
            if ([...document.querySelectorAll('input[name="amount"]')].some(visible))
                return 'AMOUNT';
            const body = document.body.innerText.toLowerCase();
            if (body.includes('dana harga tetap') && body.includes('kelas a'))
                return 'CLASS';
        } else {
            const payment = [...document.querySelectorAll('button')].some(e =>
                visible(e) && /pembayaran|payment/i.test(e.innerText));
            const agree = [...document.querySelectorAll('input[type="checkbox"][value="agree"]')].some(visible);
            if (payment && agree) return 'SUCCESS';
        }
        return null;
    """
    def check(d):
        trace_network(d)
        return d.execute_script(script, navigation)
    result = WebDriverWait(driver, timeout, poll_frequency=0.25).until(check)
    if result == 'BLOCKED':
        driver._asnb_block_retry_seconds = block_retry_seconds(
            driver.execute_script('return document.body.innerText'))
    trace_outcome(driver, 'navigation' if navigation else 'submission', result)
    return result


def find_fund_action(driver, variants):
    """Match exact fund labels and stop before crossing into another fund card."""
    return driver.execute_script("""
        const norm = s => s.toLowerCase().replace(/\\s+/g, ' ').trim();
        const fundName = s => norm(s).replace(/\\s*\\(class\\s+a\\)$/, '').trim();
        const names = arguments[0].map(norm);
        const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
        const fundLabels = [...document.querySelectorAll('span,p,h2,h3,h4')]
            .filter(e => visible(e) && /^(amanah saham malaysia|asm\\s*[23])/.test(norm(e.textContent)));
        for (const label of fundLabels.filter(e => names.includes(fundName(e.textContent)))) {
            for (let card = label.parentElement; card && card !== document.body; card = card.parentElement) {
                if (fundLabels.some(other => card.contains(other) &&
                    fundName(other.textContent) !== fundName(label.textContent))) break;
                const actions = [...card.querySelectorAll('span,button,a')].filter(e =>
                    ['add invest','pelaburan tambahan'].includes(norm(e.textContent)));
                // The whole fund card may be an anchor. Return the action itself;
                // climbing to an ancestor anchor can click the wrong navigation.
                if (actions.length) return actions[0];
            }
        }
        return null;
    """, variants)
