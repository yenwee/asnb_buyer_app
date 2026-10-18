import sys
import time
import os
import platform
import logging
import random
import signal
import shutil
import subprocess
import threading
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

# Import winsound only if on Windows to avoid import errors on other OS
if platform.system() == "Windows":
    import winsound

# --- Logging Setup ---
LOG_DIR = Path(".")

class _FlushingStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

logger = logging.getLogger("asnb_buyer")
logger.setLevel(logging.INFO)

# Console handler always active
_console_handler = _FlushingStreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(_console_handler)

_file_handler = None

def setup_logging(profile: Optional[str] = None):
    """Configure per-profile log file. Call after profile is known."""
    global _file_handler
    if _file_handler:
        logger.removeHandler(_file_handler)
        _file_handler.close()
    suffix = f"_{profile}" if profile else ""
    log_file = LOG_DIR / f"asnb_buyer{suffix}_{datetime.now().strftime('%Y%m%d')}.log"
    _file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    prefix = f"[{profile}] " if profile else ""
    _file_handler.setFormatter(logging.Formatter(f'%(asctime)s | {prefix}%(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(_file_handler)
    # Also update console format with profile prefix
    _console_handler.setFormatter(logging.Formatter(f'{prefix}%(message)s'))

# Default log file (before profile is known)
setup_logging()

# Override print to also log to file
_original_print = print
def print(*args, **kwargs):
    message = " ".join(str(a) for a in args)
    logger.info(message)

# --- Stats Tracking ---
class PurchaseStats:
    def __init__(self):
        self.attempts_per_fund = {}
        self.blocks_per_fund = {}
        self.insufficient_units_count = 0
        self.session_refreshes = 0
        self.start_time = datetime.now()

    def record_attempt(self, fund_name):
        self.attempts_per_fund[fund_name] = self.attempts_per_fund.get(fund_name, 0) + 1

    def record_block(self, fund_name):
        self.blocks_per_fund[fund_name] = self.blocks_per_fund.get(fund_name, 0) + 1

    def record_insufficient(self):
        self.insufficient_units_count += 1

    def record_refresh(self):
        self.session_refreshes += 1

    def summary(self):
        elapsed = datetime.now() - self.start_time
        lines = [f"\n--- Stats (running {str(elapsed).split('.')[0]}) ---"]
        for fund in sorted(self.attempts_per_fund.keys()):
            blocks = self.blocks_per_fund.get(fund, 0)
            lines.append(f"  {fund}: {self.attempts_per_fund[fund]} attempts, {blocks} blocks")
        lines.append(f"  Insufficient units retries: {self.insufficient_units_count}")
        lines.append(f"  Session refreshes: {self.session_refreshes}")
        return "\n".join(lines)

stats = PurchaseStats()

def bring_browser_to_front(driver):
    """Bring browser window to foreground when purchase succeeds."""
    try:
        driver.maximize_window()
        current_os = platform.system()
        if current_os == "Darwin":
            app_name = getattr(driver, "_asnb_browser_app", "Google Chrome")
            subprocess.run(
                ["osascript", "-e", f'tell application "{app_name}" to activate'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif current_os == "Windows":
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(
                    ctypes.windll.user32.GetForegroundWindow(), 9
                )
                hwnd = ctypes.windll.user32.FindWindowW(None, driver.title)
                if hwnd:
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        print("Browser brought to foreground.")
    except Exception as e:
        print(f"Could not bring browser to front: {e}")

# --- macOS Desktop Notification ---
def send_desktop_notification(title, message, sound=True):
    """Send a native macOS notification banner."""
    if platform.system() != "Darwin":
        return
    try:
        def literal(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').replace('\r', ' ') + '"'
        sound_flag = ' sound name "Glass"' if sound else ''
        script = f'display notification {literal(message)} with title {literal(title)}{sound_flag}'
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True,
                                timeout=5, check=False)
        if result.returncode:
            print('Desktop notification unavailable; runner status remains in the log.')
    except Exception as e:
        print(f"Desktop notification failed: {e}")

# --- Block Cooldown Tracking ---
class FundCooldownTracker:
    """Skip recently-blocked funds for a cooldown period to avoid wasting attempts."""
    COOLDOWN_SECONDS = 300  # Portal currently requests five minutes.

    def __init__(self):
        self._blocked_at = {}  # fund_name -> monotonic timestamp
        self._cooldown_seconds = {}

    def mark_blocked(self, fund_name, seconds=None):
        self._blocked_at[fund_name] = time.monotonic()
        self._cooldown_seconds[fund_name] = seconds if isinstance(seconds, (int, float)) and seconds > 0 else self.COOLDOWN_SECONDS

    def is_on_cooldown(self, fund_name):
        if fund_name not in self._blocked_at:
            return False
        elapsed = time.monotonic() - self._blocked_at[fund_name]
        if elapsed >= self._cooldown_seconds[fund_name]:
            del self._blocked_at[fund_name]
            del self._cooldown_seconds[fund_name]
            return False
        return True

    def remaining_cooldown(self, fund_name):
        if fund_name not in self._blocked_at:
            return 0
        elapsed = time.monotonic() - self._blocked_at[fund_name]
        return max(0, self._cooldown_seconds[fund_name] - elapsed)

fund_cooldowns = FundCooldownTracker()

# --- Periodic Summary Notification ---
SUMMARY_INTERVAL_SECONDS = 3600  # Send summary every hour
_last_summary_time = time.monotonic()
_summary_thread = None


def maybe_send_summary_email(email_config):
    """Send at most one summary in the background, without holding up purchases."""
    global _last_summary_time, _summary_thread
    if not email_config or time.monotonic() - _last_summary_time < SUMMARY_INTERVAL_SECONDS:
        return
    if _summary_thread is not None and _summary_thread.is_alive():
        return
    summary_text = stats.summary()
    summary_config = dict(email_config)
    summary_config['send_on_failure'] = True

    def send_summary():
        try:
            sent = send_purchase_notification(
                email_config=summary_config,
                success=False,
                fund_name="Periodic Status Update",
                amount="N/A",
                error_message=f"Script is still running.{summary_text}",
            )
            print("Periodic summary email sent." if sent else "Periodic summary email failed.")
        except Exception as e:
            print(f"Failed to send summary email: {e}")

    _summary_thread = threading.Thread(target=send_summary, name="summary-email", daemon=True)
    _summary_thread.start()
    _last_summary_time = time.monotonic()

_success_email_thread = None


def send_success_email_async(email_config, fund_name, amount, screenshot_paths):
    global _success_email_thread
    if _success_email_thread is not None and _success_email_thread.is_alive():
        print("Previous success email still sending; desktop notification remains active.")
        return
    def send():
        try:
            sent = send_purchase_notification(dict(email_config), success=True,
                fund_name=fund_name, amount=amount, screenshot_paths=list(screenshot_paths))
            print("Success email accepted by SMTP." if sent else "Success email failed; complete payment in browser.")
        except Exception:
            print("Success email failed; complete payment in browser.")
    _success_email_thread = threading.Thread(target=send, name="success-email", daemon=True)
    _success_email_thread.start()


def mark_fund_ui_stale(driver, fund):
    stale_funds = getattr(driver, '_asnb_stale_funds', None)
    if not isinstance(stale_funds, set):
        stale_funds = set()
        driver._asnb_stale_funds = stale_funds
    stale_funds.add(fund)


def api_preflight(driver, current_fund, funds, bank):
    """Skip known restrictions using one fresh portfolio read; preserve UI fallback."""
    client = get_api(driver)
    if client is None:
        return 'UI'
    try:
        states = client.preflight({f: fund_name_variants(f) for f in funds}, bank)
    except APISessionExpired:
        print("API session expired; login recovery required.")
        return 'SESSION_EXPIRED'
    except APIThrottled as error:
        for fund in funds:
            if not fund_cooldowns.is_on_cooldown(fund):
                fund_cooldowns.mark_blocked(fund, error.seconds)
        print(f"API transport throttled; pausing all funds for {error.seconds}s.")
        return 'SKIP'
    except APIReadError:
        if not getattr(client, '_fallback_logged', False):
            print("API read unavailable; using normal UI checks.")
            client._fallback_logged = True
        return 'UI'
    client._fallback_logged = False
    stale_funds = getattr(driver, '_asnb_stale_funds', None)
    if not isinstance(stale_funds, set):
        stale_funds = set()
        driver._asnb_stale_funds = stale_funds
    for fund, state in states.items():
        if state['state'] in ('BLOCKED', 'INACTIVE', 'CHANNEL_CLOSED', 'BANK_UNAVAILABLE'):
            if not fund_cooldowns.is_on_cooldown(fund):
                fund_cooldowns.mark_blocked(fund, state['seconds'])
                if state['state'] == 'BLOCKED':
                    stats.record_block(fund)
                print(f"API eligibility: {fund}: {state['state']}; wait {state['seconds']}s; no fund click.")
            stale_funds.add(fund)
    state = states.get(current_fund, {}).get('state')
    if state in ('BLOCKED', 'INACTIVE', 'CHANNEL_CLOSED', 'BANK_UNAVAILABLE'):
        return 'SKIP'
    if state == 'ELIGIBLE' and current_fund in stale_funds:
        driver._asnb_portfolio_refresh_needed = True
    return 'READY' if state == 'ELIGIBLE' else 'UI'


def refresh_portfolio_ui(driver, fund):
    """Keep the session alive when portfolio rendering is temporarily slow."""
    try:
        print("Refreshing portfolio UI after fund eligibility recovery.")
        driver.refresh()
        WebDriverWait(driver, 15).until(
            lambda d: find_fund_action(d, fund_name_variants(fund)))
    except (TimeoutException, WebDriverException):
        print("Portfolio refresh not ready; keeping browser session and waiting 30s before retry.")
        fund_cooldowns.mark_blocked(fund, 30)
        return False
    driver._asnb_portfolio_refresh_needed = False
    stale_funds = getattr(driver, '_asnb_stale_funds', None)
    if isinstance(stale_funds, set):
        stale_funds.clear()  # A successful page refresh replaces every fund's cached UI.
    return True


def reconcile_submission(driver):
    client = get_api(driver)
    snapshot = getattr(driver, '_asnb_submission_snapshot', None)
    if client is None or not isinstance(snapshot, dict):
        print("API transaction reconciliation unavailable; review in browser.")
        return
    try:
        print("API transaction history:", client.reconcile(snapshot))
    except APIReadError:
        print("API transaction reconciliation unavailable; review in browser.")


def wait_for_submission_review(driver):
    print("Submission outcome unknown. Check transaction history and any pending payment in the browser.")
    print("Resume only after completing/cancelling any pending transaction and confirming its outcome.")
    send_desktop_notification("ASNB - Review required", "Submission outcome unknown. Check browser before resuming.")
    print("[REVIEW_REQUIRED]")
    done, eof = threading.Event(), threading.Event()
    def read_acknowledgement():
        try:
            input("Press ENTER / Resume after reviewing, or Ctrl+C to stop: ")
        except EOFError:
            eof.set()
        finally:
            done.set()
    threading.Thread(target=read_acknowledgement, name="submission-review", daemon=True).start()
    while not done.wait(timeout=1):
        trace_network(driver)
    if eof.is_set():
        raise KeyboardInterrupt()

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.common.exceptions import WebDriverException, TimeoutException, NoSuchElementException
from .popups import dismiss_login_popups
from .purchase_state import find_fund_action, wait_purchase_state
from .health import BrowserHealth
from .api import PortalAPI, APIReadError, APISessionExpired, APIThrottled, get_api
from .network import trace_network, network_summary
from .purchase_state import submit_and_observe, block_retry_seconds
import hashlib
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

try:
    from asnb.config import load_config, get_funds_list, get_email_config, get_session_refresh_interval, get_profile_names, ConfigError
    from asnb.driver import get_webdriver, close_webdriver, DriverSetupError
    from asnb.email import send_purchase_notification, EmailNotificationError
    from asnb.actions import human_type, human_click, human_js_click, human_delay, human_scroll_to, between_actions, after_page_load
except ImportError as e:
    print(f"Error importing modules: {e}")
    print("Ensure the asnb package is on the Python path (run with python -m asnb.main).")
    sys.exit(1)

# --- Constants ---
DEFAULT_PURCHASE_TRANSPORT = 'api'
# MYASNB_URL = "https://www.myasnb.com.my/" # Original URL, user specified login page
MYASNB_LOGIN_URL = "https://www.myasnb.com.my/login"
MYASNB_PORTFOLIO_URL = "https://www.myasnb.com.my/portfolio"
MYASNB_LOGOUT_URL = "https://www.myasnb.com.my/logout"
MYASNB_HOME_URL = "https://www.myasnb.com.my/"
WAIT_TIMEOUT = 5 # Increased timeout for potentially slow elements

# --- Helper Function for Explicit Waits ---
def wait_for_element(driver: WebDriver, by: By, value: str, timeout: int = WAIT_TIMEOUT):
    """Waits for an element to be present and returns it."""
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, value)))

def wait_for_clickable_element(driver: WebDriver, by: By, value: str, timeout: int = WAIT_TIMEOUT):
    """Waits for an element to be clickable and returns it."""
    return WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, value)))

def wait_for_clickable_any(driver: WebDriver, candidates, timeout: int = WAIT_TIMEOUT, description: str = "element"):
    """Wait for the first displayed/enabled element from a list of selector candidates."""
    def find_clickable(d):
        trace_network(d)
        for label, by, value in candidates:
            try:
                for element in d.find_elements(by, value):
                    if element.is_displayed() and element.is_enabled():
                        return label, element
            except WebDriverException:
                continue
        return False

    try:
        return WebDriverWait(driver, timeout).until(find_clickable)
    except TimeoutException as exc:
        tried = "; ".join(f"{label}: {value}" for label, _, value in candidates)
        raise TimeoutException(f"Timed out waiting for {description}. Tried: {tried}") from exc

def normalized_text(value: str) -> str:
    """Normalize UI text for stable comparisons across whitespace/case changes."""
    return " ".join((value or "").split()).casefold()

def page_contains_text(driver: WebDriver, expected_text: str) -> bool:
    """Return whether the current visible page text contains expected_text."""
    if not expected_text:
        return True
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
    except WebDriverException:
        return False
    return normalized_text(expected_text) in normalized_text(page_text)

FUND_NAME_ALIASES = {
    "Amanah Saham Malaysia 2": [
        "Amanah Saham Malaysia 2 Wawasan",
        "Amanah Saham Malaysia 2 - Wawasan",
        "ASM 2 Wawasan",
        "ASM 2 - Wawasan",
        "ASM 2",
    ],
    "Amanah Saham Malaysia 2 Wawasan": [
        "Amanah Saham Malaysia 2",
        "Amanah Saham Malaysia 2 - Wawasan",
        "ASM 2 Wawasan",
        "ASM 2 - Wawasan",
        "ASM 2",
    ],
}

def fund_name_variants(fund_name: str) -> list:
    """Return portal text aliases for a configured fund name."""
    variants = []
    for candidate in [fund_name, *FUND_NAME_ALIASES.get(fund_name, [])]:
        cleaned = " ".join((candidate or "").split())
        if cleaned and cleaned not in variants:
            variants.append(cleaned)
    lowered = normalized_text(fund_name)
    if "wawasan" in lowered or "malaysia 2" in lowered or "asm 2" in lowered:
        for candidate in ["Amanah Saham Malaysia 2", *FUND_NAME_ALIASES["Amanah Saham Malaysia 2"]]:
            cleaned = " ".join(candidate.split())
            if cleaned not in variants:
                variants.append(cleaned)
    return variants

def find_add_invest_for_fund(driver: WebDriver, fund_name: str, timeout: int = WAIT_TIMEOUT):
    """Find the Add Invest/Pelaburan Tambahan control for a fund, including aliases."""
    variants = fund_name_variants(fund_name)
    print(f"Fund lookup variants: {variants}")

    return WebDriverWait(driver, timeout, poll_frequency=0.25).until(
        lambda d: find_fund_action(d, variants))

def select_fixed_price_class_a(driver: WebDriver) -> bool:
    """Select Kelas A when MyASNB shows the fixed-price fund class modal."""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except WebDriverException:
        return False
    normalized_body = normalized_text(body_text)
    if "dana harga tetap" not in normalized_body or "kelas a" not in normalized_body:
        return False

    print("Fixed-price class modal detected. Selecting Kelas A...")
    script = """
        const textOf = (el) => (el.innerText || el.textContent || '')
            .toLowerCase().replace(/\\s+/g, ' ').trim();
        const labels = Array.from(document.querySelectorAll('span, p, div, button'))
            .filter(el => textOf(el) === 'kelas a' || textOf(el).startsWith('kelas a '));
        for (const label of labels) {
            let current = label;
            for (let depth = 0; current && depth < 8; depth += 1) {
                const text = textOf(current);
                const style = window.getComputedStyle(current);
                const clickable = current.tagName === 'BUTTON'
                    || current.tagName === 'A'
                    || current.getAttribute('role') === 'button'
                    || style.cursor === 'pointer'
                    || typeof current.onclick === 'function';
                if (text.includes('kelas a') && clickable) {
                    return current;
                }
                current = current.parentElement;
            }
        }
        for (const label of labels) {
            let current = label;
            for (let depth = 0; current && depth < 6; depth += 1) {
                const text = textOf(current);
                if (text.includes('kelas a') && text.includes('pemegang unit')) {
                    return current;
                }
                current = current.parentElement;
            }
        }
        return null;
    """
    try:
        class_a_element = driver.execute_script(script)
        if not class_a_element:
            print("Kelas A option not found in modal.")
            return False
        human_scroll_to(driver, class_a_element)
        human_js_click(driver, class_a_element)
        print("Kelas A selected.")
        between_actions()
        return True
    except WebDriverException as e:
        print(f"Failed to select Kelas A: {e}")
        return False

def detect_and_dismiss_block_popup(driver: WebDriver) -> Union[bool, str]:
    """Inspect current visible errors without serial selector timeouts."""
    text = normalized_text(driver.find_element(By.TAG_NAME, "body").text)
    if not any(token in text for token in (
        "blocked due to", "too many request", "try again in 5 minutes",
        "(1001)", "insufficient units available", "(068)",
    )):
        return False
    print("Block/error popup detected at navigation stage!")
    driver._asnb_block_retry_seconds = block_retry_seconds(text)
    _, button = wait_for_clickable_any(driver, [
        ("Close/OK", By.XPATH, "//button[normalize-space()='Close' or normalize-space()='Tutup' or normalize-space()='OK' or normalize-space()='Ok']"),
        ("Labelled close", By.CSS_SELECTOR, "[aria-label='Close'], [aria-label='Tutup'], [title='Close'], [title='Tutup']"),
        ("SVG close", By.CSS_SELECTOR, "div.fixed.inset-0 svg.absolute.cursor-pointer"),
    ], description="block popup close")
    driver.execute_script("""
        arguments[0].dispatchEvent(new MouseEvent('click', {
            view: window, bubbles: true, cancelable: true
        }));
    """, button)
    print("Dismissed block popup.")
    return "BLOCKED" if any(t in text for t in ("(1001)", "blocked due to", "too many request", "try again in 5 minutes")) else "INSUFFICIENT"

# --- Debug Snapshot on Login Failure ---
DEBUG_DIR = Path("debug_snapshots")

def save_debug_snapshot(driver: WebDriver, reason: str, context: str = "unknown"):
    """Captures screenshot, page source, URL, and visible errors on failure."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{context}_{timestamp}"

        # Screenshot
        screenshot_path = DEBUG_DIR / f"{prefix}.png"
        driver.save_screenshot(str(screenshot_path))
        print(f"[DEBUG] Screenshot saved: {screenshot_path}")

        # Collect debug info
        current_url = driver.current_url
        page_title = driver.title

        # Grab all visible error/warning text on page
        visible_errors = []
        error_selectors = [
            "//div[contains(@class, 'text-danger') or contains(@class, 'error') or contains(@class, 'alert')]",
            "//p[contains(@class, 'text-danger') or contains(@class, 'error')]",
            "//span[contains(@class, 'text-danger') or contains(@class, 'error')]",
            "//div[contains(@class, 'toast') or contains(@class, 'notification')]",
        ]
        for sel in error_selectors:
            try:
                elements = driver.find_elements(By.XPATH, sel)
                for el in elements:
                    txt = el.text.strip()
                    if txt and txt not in visible_errors:
                        visible_errors.append(txt)
            except Exception:
                pass

        # Check for common known error patterns in page source
        page_source = driver.page_source
        known_patterns = [
            "sesi yang masih aktif",
            "active session",
            "invalid credentials",
            "kata laluan tidak sah",
            "account locked",
            "akaun dikunci",
            "maintenance",
            "penyelenggaraan",
            "try again later",
            "cuba semula",
        ]
        source_matches = [p for p in known_patterns if p.lower() in page_source.lower()]

        # Write debug report
        report_path = DEBUG_DIR / f"{prefix}_report.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"Debug Report [{context}]\n")
            f.write(f"{'=' * 50}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Reason: {reason}\n")
            f.write(f"Current URL: {current_url}\n")
            f.write(f"Page Title: {page_title}\n\n")
            f.write(f"Visible Errors/Warnings:\n")
            if visible_errors:
                for err in visible_errors:
                    f.write(f"  - {err}\n")
            else:
                f.write(f"  (none detected)\n")
            f.write(f"\nKnown Pattern Matches in Page Source:\n")
            if source_matches:
                for m in source_matches:
                    f.write(f"  - '{m}'\n")
            else:
                f.write(f"  (none detected)\n")
            f.write(f"\n{'=' * 50}\n")
            f.write(f"Page Source (first 5000 chars):\n")
            f.write(page_source[:5000])
            f.write(f"\n")

        print(f"[DEBUG] Report saved: {report_path}")
        print(f"[DEBUG] URL at failure: {current_url}")
        if visible_errors:
            print(f"[DEBUG] Visible errors: {visible_errors}")
        if source_matches:
            print(f"[DEBUG] Known patterns found: {source_matches}")
    except Exception as debug_err:
        print(f"[DEBUG] Failed to save debug snapshot: {debug_err}")

# --- Updated Login Function ---

def login(driver: WebDriver, username: str, password: str, security_phrase: str = "") -> bool:
    """
    Attempts to log into the MyASNB website.

    Args:
        driver: The initialized Selenium WebDriver instance.
        username: The user's MyASNB username.
        password: The user's MyASNB password.
        security_phrase: Unused; kept for config compatibility.

    Returns:
        True if login is successful (or appears successful), False otherwise.
    """
    """
    Attempts to log into the MyASNB website using the specified flow.

    Args:
        driver: The initialized Selenium WebDriver instance.
        username: The user's MyASNB username.
        password: The user's MyASNB password.
        security_phrase: Unused; kept for config compatibility.

    Returns:
        True if login is successful, False otherwise.
    """
    print("Attempting to log in...")
    try:
        # 1. Navigate to Login Page
        driver.get(MYASNB_LOGIN_URL)
        print(f"Navigated to {MYASNB_LOGIN_URL}")

        # Wait for optional announcements and verify that the form is reachable.
        # Eager navigation can return before React mounts the login form.
        wait_for_element(driver, By.NAME, "username", timeout=30)
        dismiss_login_popups(driver)

        # 3. Enter Username (human-like typing)
        print("Entering username...")
        username_field = wait_for_element(driver, By.NAME, "username")
        human_type(username_field, username)
        print("Username entered.")
        between_actions()

        # 4. Click First Continue Button (Teruskan)
        print("Clicking 'Teruskan' button...")
        continue_button_candidates = [
            ("ID - btnContinue", By.ID, "btnContinue"),
            ("Username form submit", By.XPATH, "//form[.//input[@name='username']]//button[@type='submit']"),
            ("Malay/English continue text", By.XPATH, "//button[normalize-space()='Teruskan' or normalize-space()='Continue' or .//span[normalize-space()='Teruskan'] or .//span[normalize-space()='Continue']]"),
        ]
        selector_name, continue_button_1 = wait_for_clickable_any(
            driver,
            continue_button_candidates,
            timeout=WAIT_TIMEOUT * 2,
            description="'Teruskan' button",
        )
        print(f"Found 'Teruskan' button using: {selector_name}")
        dismiss_login_popups(driver, target=continue_button_1, appearance_wait=0)
        human_click(driver, continue_button_1)
        print("'Teruskan' button clicked.")

        # 5. Verify the security phrase, then click "Yes"/"Ya"
        print("Waiting for security phrase confirmation...")
        yes_button_candidates = [
            ("ID - btnYes", By.ID, "btnYes"),
            ("Malay/English yes text", By.XPATH, "//button[normalize-space()='Ya' or normalize-space()='Yes' or .//span[normalize-space()='Ya'] or .//span[normalize-space()='Yes']]"),
        ]
        selector_name, yes_button = wait_for_clickable_any(
            driver,
            yes_button_candidates,
            timeout=WAIT_TIMEOUT * 2,
            description="'Yes'/'Ya' confirmation button",
        )
        print(f"Found confirmation button using: {selector_name}")
        print("Skipping security phrase comparison; continuing to password entry.")
        print("Clicking 'Yes'/'Ya' button...")
        dismiss_login_popups(driver, target=yes_button, appearance_wait=0)
        human_click(driver, yes_button)
        print("'Yes'/'Ya' button clicked.")
        between_actions()

        # 6. Enter Password (human-like typing)
        print("Entering password...")
        password_field = wait_for_element(driver, By.NAME, "password")
        human_type(password_field, password)
        print("Password entered.")
        between_actions()

        # 7. Click Login Button
        print("Clicking 'Login'...")
        login_button_candidates = [
            ("Password form submit", By.XPATH, "//form[.//input[@name='password']]//button[@type='submit']"),
            ("ID - no-printable", By.ID, "no-printable"),
            ("Malay/English login text", By.XPATH, "//button[normalize-space()='Log Masuk' or normalize-space()='Login' or .//span[normalize-space()='Log Masuk'] or .//span[normalize-space()='Login']]"),
        ]
        selector_name, login_button = wait_for_clickable_any(
            driver,
            login_button_candidates,
            timeout=WAIT_TIMEOUT * 2,
            description="'Login'/'Log Masuk' button",
        )
        print(f"Found 'Login' button using: {selector_name}")
        dismiss_login_popups(driver, target=login_button, appearance_wait=0)
        human_click(driver, login_button)
        print("'Login' button clicked.")

        # 8. Check for active session error before verifying URL redirect
        try:
            print("Checking for active session conflict error...")
            # Look for the active session error message
            active_session_error_xpath = "//div[contains(@class, 'text-danger50') and contains(text(), 'Anda mempunyai sesi yang masih aktif')]"
            active_session_element = driver.find_element(By.XPATH, active_session_error_xpath)
            if active_session_element.is_displayed():
                error_text = active_session_element.text
                print(f"Active session detected: {error_text}")

                # Try to force-kill the stale session by hitting /logout
                print("Attempting to clear stale session via /logout...")
                try:
                    driver.get(MYASNB_LOGOUT_URL)
                    time.sleep(3)
                    current = driver.current_url
                    if '/logout' in current or '/login' in current:
                        print("Stale session cleared via /logout. Retrying login...")
                        time.sleep(2)
                        return False  # Retry login immediately
                except Exception as e:
                    print(f"Force logout attempt failed: {e}")

                # Fallback: wait for server-side session timeout
                print("Waiting 60 seconds for server to release session...")
                for remaining in range(60, 0, -10):
                    print(f"Waiting... {remaining} seconds remaining")
                    time.sleep(10)

                print("Session wait completed. Returning False to trigger retry...")
                save_debug_snapshot(driver, f"Active session conflict: {error_text}", "login")
                return False  # Return False to indicate login should be retried
        except NoSuchElementException:
            # No active session error found, continue with normal login verification
            pass

        # 9. Verify Login by URL
        print(f"Waiting for redirection to portfolio page ({MYASNB_PORTFOLIO_URL})...")
        try:
            WebDriverWait(driver, WAIT_TIMEOUT * 2).until(EC.url_to_be(MYASNB_PORTFOLIO_URL))
            print("Login successful: Redirected to portfolio page.")
            print("Pausing briefly after login...")
            after_page_load(2.0, 4.0)
            if get_api(driver):
                get_api(driver).reset()
            return True
        except TimeoutException:
            # Check again for active session error after timeout
            try:
                active_session_element = driver.find_element(By.XPATH, active_session_error_xpath)
                if active_session_element.is_displayed():
                    print("Active session error detected after timeout. Will retry after wait.")
                    save_debug_snapshot(driver, "Active session error after URL redirect timeout", "login")
                    return False  # Trigger retry
            except NoSuchElementException:
                pass
            # Re-raise the original timeout exception if no session error
            raise

    except TimeoutException as e:
        print(f"Error during login: Timed out waiting for element. {e}")
        save_debug_snapshot(driver, f"TimeoutException: {e}", "login")
        return False
    except NoSuchElementException as e:
        print(f"Error during login: Could not find expected element. {e}")
        save_debug_snapshot(driver, f"NoSuchElementException: {e}", "login")
        return False
    except WebDriverException as e:
        print(f"WebDriver error during login: {e}")
        save_debug_snapshot(driver, f"WebDriverException: {e}", "login")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during login: {e}")
        save_debug_snapshot(driver, f"UnexpectedException: {e}", "login")
        return False

def navigate_to_purchase(driver: WebDriver, fund_to_buy: Optional[str]) -> Union[bool, str]:
    """
    Navigates from the dashboard to the fund purchase section for a specific fund.

    Args:
        driver: The initialized Selenium WebDriver instance.

    Returns:
        True if navigation is successful, False otherwise.
    """
    print(f"Navigating to purchase section for fund: {fund_to_buy}...")
    if not fund_to_buy:
        print("Error: Fund name not specified for navigation.")
        return False

    try:
        add_invest_span = find_add_invest_for_fund(driver, fund_to_buy, timeout=WAIT_TIMEOUT * 2)
        print(f"Found exact fund action for '{fund_to_buy}'.")

        # Scroll element into view, then click with human-like behavior
        try:
            print("Scrolling 'Add Invest' span into view...")
            human_scroll_to(driver, add_invest_span)
            print("Attempting JavaScript click on 'Add Invest' span...")
            human_js_click(driver, add_invest_span)
            print("JavaScript click executed on 'Add Invest' span.")
        except Exception as js_click_err:
            print(f"JavaScript click failed ({js_click_err}). Navigation might fail.")
            raise WebDriverException(f"Failed to click 'Add Invest' span via JavaScript: {js_click_err}")

        # --- Verification step: Wait for the amount input field to appear ---
        amount_input_xpath = "//input[@name='amount' and @type='text']"
        try:
            print(f"Verifying navigation by waiting for amount input: {amount_input_xpath}")
            nav_state = wait_purchase_state(driver, navigation=True)
            if nav_state != "AMOUNT":
                raise TimeoutException(f"Navigation outcome: {nav_state}")
            print("Successfully navigated to purchase page (amount input found).")
            return True
        except TimeoutException:
            print("Amount input not found. Checking for block popup at navigation stage...")

            if select_fixed_price_class_a(driver):
                try:
                    print("Waiting for amount input after selecting Kelas A...")
                    wait_for_element(driver, By.XPATH, amount_input_xpath, timeout=WAIT_TIMEOUT * 2)
                    print("Successfully navigated to purchase page after selecting Kelas A.")
                    return True
                except TimeoutException:
                    print("Amount input still not found after selecting Kelas A. Retrying fund action once...")
                    try:
                        add_invest_span = find_add_invest_for_fund(driver, fund_to_buy, timeout=WAIT_TIMEOUT)
                        human_scroll_to(driver, add_invest_span)
                        human_js_click(driver, add_invest_span)
                        wait_for_element(driver, By.XPATH, amount_input_xpath, timeout=WAIT_TIMEOUT * 2)
                        print("Successfully navigated to purchase page after Kelas A retry.")
                        return True
                    except Exception as retry_err:
                        print(f"Kelas A retry did not reach amount input: {retry_err}")

            nav_error = detect_and_dismiss_block_popup(driver)
            if nav_error:
                return nav_error

            print("No block popup found either. Navigation failed for unknown reason.")
            save_debug_snapshot(driver, f"Navigation failed after clicking Add Invest for {fund_to_buy}", "navigate_purchase")
            return False

    except (TimeoutException, NoSuchElementException) as e:
        print(f"Error during navigation: Could not find expected element. {e}")
        return False
    except WebDriverException as e:
        print(f"WebDriver error during navigation: {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during navigation: {e}")
        return False

# --- Logout Function ---
def logout(driver: WebDriver) -> bool:
    """Logs out from the current session by navigating directly to the logout URL."""
    print("Attempting to log out...")
    try:
        # Check if WebDriver is still alive
        try:
            current_url = driver.current_url
        except Exception:
            print("WebDriver is dead, skipping logout.")
            return False

        # Already logged out? (on login page or public homepage)
        if current_url.rstrip('/') in [MYASNB_LOGIN_URL, MYASNB_HOME_URL.rstrip('/')]:
            print(f"Already on {current_url} - no logout needed.")
            return True

        # Primary strategy: navigate directly to /logout URL
        # This works regardless of what page we're on (portfolio, transactions, FPX redirect, etc.)
        print(f"Navigating to {MYASNB_LOGOUT_URL}...")
        driver.get(MYASNB_LOGOUT_URL)

        # Verify logout succeeded
        try:
            WebDriverWait(driver, WAIT_TIMEOUT * 2).until(
                lambda d: '/logout' in d.current_url or
                         '/login' in d.current_url or
                         'logged out' in d.page_source.lower() or
                         d.find_elements(By.NAME, 'username')
            )
            print("Logout confirmed.")

            # Dismiss satisfaction survey if present
            try:
                close_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Close') or contains(text(), 'Tutup')]")
                close_btn.click()
                print("Dismissed post-logout survey.")
            except (NoSuchElementException, WebDriverException):
                pass

            time.sleep(1)
            return True
        except TimeoutException:
            # Check page state to determine if logout actually worked
            post_url = driver.current_url
            print(f"Logout verification timed out. Current URL: {post_url}")
            save_debug_snapshot(driver, f"Logout verification timeout at {post_url}", "logout_unconfirmed")
            # If we're not on an authenticated page, logout likely worked
            if '/portfolio' not in post_url and '/transactions' not in post_url:
                return True
            return False

    except WebDriverException as e:
        print(f"WebDriver error during logout: {e}")
        # Last resort: clear cookies to force client-side session teardown
        try:
            print("Clearing cookies as fallback...")
            driver.delete_all_cookies()
            return True
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"Unexpected error during logout: {e}")
        save_debug_snapshot(driver, f"Logout exception: {e}", "logout")
        return False

# --- Verify Fund Loading ---
def verify_funds_loaded(driver: WebDriver, expected_funds: list, timeout: int = WAIT_TIMEOUT) -> bool:
    """
    Verifies that funds are properly loaded in the portfolio page.
    
    Args:
        driver: WebDriver instance
        expected_funds: List of fund names to check for
        timeout: Maximum time to wait for funds to load
    
    Returns:
        True if funds are properly loaded, False otherwise
    """
    print("Verifying that funds are properly loaded in portfolio...")
    
    try:
        # Wait for the main portfolio content to load
        print("Waiting for portfolio content to load...")
        
        # Common selectors for fund elements (you may need to adjust these)
        fund_loading_selectors = [
            # Look for fund cards/containers
            "//div[contains(@class, 'fund') or contains(@class, 'card')]",
            # Look for "Add Invest" or "Pelaburan Tambahan" buttons
            "//span[contains(text(), 'Pelaburan Tambahan') or contains(text(), 'Add Invest')]",
            # Look for fund names
            "//span[contains(text(), 'Amanah Saham')]",
            # Look for amount/balance elements
            "//div[contains(text(), 'RM') or contains(@class, 'balance')]",
        ]
        
        funds_detected = False
        
        for selector_name, selector in [("Fund containers", fund_loading_selectors[0]), 
                                      ("Investment buttons", fund_loading_selectors[1]),
                                      ("Fund names", fund_loading_selectors[2])]:
            try:
                print(f"Checking for {selector_name}...")
                elements = WebDriverWait(driver, timeout).until(
                    lambda d: d.find_elements(By.XPATH, selector)
                )
                if len(elements) > 0:
                    print(f"✓ Found {len(elements)} {selector_name.lower()}")
                    funds_detected = True
                    break
            except TimeoutException:
                print(f"✗ No {selector_name.lower()} found")
                continue
        
        # Check for skeleton/loading state (gray placeholder bars)
        skeleton_elements = driver.find_elements(By.XPATH, "//div[contains(@class, 'animate-pulse')] | //div[contains(@style, 'background') and contains(@style, 'gray')] | //*[contains(@class, 'skeleton')]")
        if len(skeleton_elements) > 3:  # More than 3 skeleton elements indicates loading state
            print(f"⚠️ Detected {len(skeleton_elements)} skeleton/loading elements - portfolio still loading")
            return False
        
        # Check if page shows transaction records header but no actual fund data (common stale session symptom)
        transaction_header = driver.find_elements(By.XPATH, "//*[contains(text(), 'Rekod Urusniaga Terkini') or contains(text(), 'Recent Transaction')]")
        if transaction_header and not funds_detected:
            print("⚠️ Found transaction header but no fund data - likely stale session")
            return False
        
        if not funds_detected:
            print("❌ No fund elements detected - portfolio may not have loaded properly")
            return False
        
        # Additional check: Try to find at least one of the expected funds
        funds_found = 0
        for fund_name in expected_funds[:3]:  # Check first 3 funds to avoid too many searches
            try:
                fund_elements = []
                for fund_variant in fund_name_variants(fund_name):
                    fund_xpath = f"//span[contains(text(), '{fund_variant}')]"
                    fund_elements = driver.find_elements(By.XPATH, fund_xpath)
                    if fund_elements:
                        break
                if fund_elements:
                    funds_found += 1
                    print(f"✓ Found fund: {fund_name} (matched: {fund_variant})")
            except Exception as e:
                print(f"⚠️ Error checking fund {fund_name}: {e}")
        
        if funds_found > 0:
            print(f"✅ Portfolio verification successful - {funds_found} expected funds found")
            return True
        else:
            print("⚠️ Portfolio loaded but expected funds not found - may need session refresh")
            return False
            
    except TimeoutException:
        print("❌ Portfolio content did not load within timeout period")
        return False
    except Exception as e:
        print(f"❌ Error verifying portfolio loading: {e}")
        return False

# --- Navigate Back to Portfolio ---
def navigate_to_portfolio(driver: WebDriver) -> bool:
    """Navigates back to the main portfolio/account page."""
    print("Navigating back to portfolio page...")
    try:
        # Target selectors in order of reliability (tested and working)
        my_account_selectors = [
            # Primary - JavaScript click on span (TESTED WORKING)
            ("Span - JavaScript click", By.XPATH, "//span[contains(@class, 'text-white') and contains(text(), 'My Account')]"),
            ("Span - JavaScript click (Malay)", By.XPATH, "//span[contains(@class, 'text-white') and contains(text(), 'Akaun Saya')]"),
            
            # Fallback - try anchor elements
            ("Anchor with My Account span", By.XPATH, "//a[.//span[contains(text(), 'My Account')]]"),
            ("Anchor with Akaun Saya span", By.XPATH, "//a[.//span[contains(text(), 'Akaun Saya')]]"),
        ]
        
        successful_selector, my_account_element = wait_for_clickable_any(
            driver, my_account_selectors, description="My Account/Akaun Saya")

        print("Clicking 'My Account' element...")
        
        # If it's a span selector, use JavaScript click for reliability
        if "Span" in successful_selector:
            try:
                print("Using JavaScript click for span element...")
                driver.execute_script("arguments[0].click();", my_account_element)
                print("JavaScript click executed successfully.")
            except Exception as js_err:
                print(f"JavaScript click failed ({js_err}), trying direct click...")
                my_account_element.click()
        else:
            # For anchor elements, regular click should work
            my_account_element.click()
        
        WebDriverWait(driver, WAIT_TIMEOUT * 2, poll_frequency=0.25).until(
            EC.url_to_be(MYASNB_PORTFOLIO_URL))
        WebDriverWait(driver, WAIT_TIMEOUT * 2, poll_frequency=0.25).until(
            lambda d: d.execute_script("""
                return [...document.querySelectorAll('span')].some(e =>
                    e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' &&
                    /^(amanah saham malaysia|asm\\s*[23])/i.test(e.textContent.trim()));
            """))
        print("Successfully navigated back to portfolio page.")
        return True
        
    except (TimeoutException, NoSuchElementException, WebDriverException) as e:
        print(f"Error navigating back to portfolio: {e}")
        return False


# --- Modified Purchase Unit Function ---
def pause_for_payment(driver, fund_name, amount, email_config=None):
    """Notify once, then wait for manual final agreement and FPX completion."""
    # Bring browser to foreground for manual payment
    bring_browser_to_front(driver)

    # Play platform-specific success notification + desktop banner
    success_message = f"Success! You have RM{amount} amount of {fund_name} units available for purchase. Please complete the payment."
    print(f"\n*** {success_message} ***\n")
    send_desktop_notification("ASNB Purchase Ready!", f"RM{amount} of {fund_name} - Complete payment now!", sound=True)

    # Take screenshot for email attachment
    screenshot_paths = []
    if email_config:
        try:
            screenshot_filename = f"asnb_success_{fund_name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            screenshot_path = os.path.join(os.getcwd(), screenshot_filename)
            driver.save_screenshot(screenshot_path)
            screenshot_paths = [screenshot_path]
            print(f"📸 Screenshot saved: {screenshot_filename}")
        except Exception as screenshot_err:
            print(f"Warning: Could not take screenshot: {screenshot_err}")

    # SMTP must not delay the payment prompt or desktop notification.
    if email_config:
        send_success_email_async(email_config, fund_name, amount, screenshot_paths)

    try:
        current_os = platform.system()
        if current_os == "Darwin": # macOS
            print("Playing macOS notification sound...")
            subprocess.run(["say", success_message])
        elif current_os == "Windows":
            print("Playing Windows notification sound...")
            # Play the default system asterisk sound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            # Optionally, play a specific frequency/duration beep
            # winsound.Beep(1000, 500) # Frequency 1000 Hz, Duration 500 ms
        elif current_os == "Linux":
            # Try spd-say first (common text-to-speech dispatcher)
            if shutil.which("spd-say"):
                print("Playing Linux notification sound via spd-say...")
                os.system(f'spd-say "{success_message}"')
            # Fallback: Try espeak if available (another common TTS engine)
            elif shutil.which("espeak"):
                 print("Playing Linux notification sound via espeak...")
                 os.system(f'espeak "{success_message}"')
            else:
                print("No standard Linux notification command found (spd-say/espeak). Printing only.")
                # Add other Linux sound commands here if needed (e.g., paplay, aplay)
        else:
            print(f"Unsupported OS ({current_os}) for sound notification.")

        # Wait for user to complete payment and choose next action
        print("\n--- Script paused. Please complete the payment manually in the browser. ---")
        print("--- After payment, press ENTER to resume buying remaining funds. ---")
        print("--- Or press Ctrl+C to stop the script. ---")

    except Exception as sound_err:
        print(f"Could not play notification sound: {sound_err}")

    try:
        print("\n>>> Press ENTER after completing payment to resume, or Ctrl+C to quit.")
        print("[RESUME_READY]")
        sys.stdout.flush()
        # Threaded stdin reader so SIGTERM can interrupt (works on all platforms)
        _resume_event = threading.Event()
        _resume_eof = threading.Event()
        def _stdin_reader():
            try:
                line = sys.stdin.readline()
                if not line:  # EOF - pipe closed (GUI died)
                    _resume_eof.set()
                _resume_event.set()
            except (EOFError, OSError):
                _resume_eof.set()
                _resume_event.set()
        threading.Thread(target=_stdin_reader, daemon=True).start()
        while not _resume_event.is_set():
            _resume_event.wait(timeout=1.0)
            trace_network(driver)
        if _resume_eof.is_set():
            print("\n--- Stdin closed (GUI exited?). Stopping. ---")
            raise KeyboardInterrupt()
        print("Resuming automation after payment...")
        return "RESUME"
    except (KeyboardInterrupt, EOFError):
        print("\n--- User chose to stop. ---")
        raise KeyboardInterrupt()


def purchase_unit(driver: WebDriver, fund_name: Optional[str], amount: Optional[str], bank_name: str, email_config: dict = None, referral_code: str = '') -> Union[bool, str]:
    """
    Executes the purchase of units for a specified fund. Handles retry logic for insufficient units
    and detects the 'Blocked' error. Uses the specified bank name.

    Args:
        driver: The initialized Selenium WebDriver instance.
        fund_name: The name of the fund being purchased (for logging).
        amount: The amount to invest.
        referral_code: Optional referral/campaign code (Kod Rujukan) to fill in.

    Returns:
        True: If the purchase reaches the final confirmation page.
        "RESUME": If the purchase succeeded and user wants to continue with remaining funds.
        "BLOCKED": If the 'Blocked due to retry' (1001) error occurs.
        "UNCERTAIN": A submission may have been dispatched; manual review required.
        False: If any other error occurs or retries are exhausted.
    """
    print(f"Attempting to purchase units...")
    # Amount should be loaded from config in main() and passed here
    if not amount:
        print("Purchase amount not provided. Skipping purchase.")
        return False # Amount is required

    # fund_name is determined by the navigation step, not needed here unless verifying
    print(f"Amount to invest: {amount}")
    submission_attempted = False
    driver._asnb_submission_snapshot = None
    try:
        api_transport = getattr(driver, '_asnb_purchase_transport', DEFAULT_PURCHASE_TRANSPORT) == 'api'
        next_button = None
        if not api_transport:
            # 1. Wait for and fill amount input
            amount_input_xpath = "//input[@name='amount' and @type='text']"
            print(f"Waiting for amount input: {amount_input_xpath}")
            amount_field = wait_for_element(driver, By.XPATH, amount_input_xpath)
            print(f"Entering amount: {amount}")
            human_type(amount_field, str(amount))
            print("Amount entered.")
            between_actions()

            # 2. Wait for and select bank from dropdown using the configured bank name
            bank_select_xpath = "//select[@name='banks']"
            print(f"Waiting for bank dropdown: {bank_select_xpath}")
            bank_dropdown_element = wait_for_element(driver, By.XPATH, bank_select_xpath)

            # Wait for dropdown options to populate (they load asynchronously)
            bank_option_xpath = "//select[@name='banks']/option[not(@value='') and not(contains(text(), 'Pilih'))]"
            for wait_attempt in range(10):
                bank_dropdown = Select(bank_dropdown_element)
                if len(bank_dropdown.options) > 1:
                    print(f"Bank dropdown populated with {len(bank_dropdown.options)} options.")
                    break
                print(f"Waiting for bank options to load... (attempt {wait_attempt + 1}/10)")
                time.sleep(1)
            else:
                print("Warning: Bank dropdown options did not populate after 10 seconds.")

            bank_dropdown = Select(bank_dropdown_element)
            print(f"Attempting to select bank: {bank_name}")
            try:
                bank_dropdown.select_by_visible_text(bank_name)
                print(f"Bank '{bank_name}' selected successfully.")
            except NoSuchElementException:
                print(f"Warning: Bank '{bank_name}' not found in dropdown. Defaulting to 'Public Bank'.")
                try:
                    bank_dropdown.select_by_visible_text("Public Bank")
                    print("Default bank 'Public Bank' selected.")
                except NoSuchElementException:
                    print("Error: Default bank 'Public Bank' also not found. Cannot proceed.")
                    return False # Critical error if default bank isn't there

            # 3. Fill referral code if configured
            if referral_code:
                print(f"Filling referral code (Kod Rujukan): {referral_code}")
                try:
                    referral_input = driver.find_element(By.XPATH, "//input[@name='referral']")
                    human_type(referral_input, referral_code)
                    print(f"Referral code '{referral_code}' entered.")
                except NoSuchElementException:
                    print("Warning: Referral code field (name='referral') not found on page. Skipping.")
                between_actions()

            # 4. Wait for and click agreement checkbox
            checkbox_xpath = "//input[@type='checkbox' and @value='agree']"
            print(f"Waiting for agreement checkbox: {checkbox_xpath}")
            checkbox = wait_for_clickable_element(driver, By.XPATH, checkbox_xpath)
            # Use JavaScript click for potentially tricky checkboxes
            try:
                print("Attempting JavaScript click on checkbox...")
                driver.execute_script("arguments[0].click();", checkbox)
                print("Checkbox clicked via JavaScript.")
            except Exception as cb_click_err:
                 print(f"JavaScript click on checkbox failed ({cb_click_err}), trying direct click...")
                 checkbox.click() # Fallback to direct click
                 print("Checkbox clicked directly.")


            # Check both languages together instead of timing out on the absent one.
            next_button_xpath = "//button[@type='submit' and (contains(., 'Seterusnya') or contains(., 'Next'))]"
            next_button = wait_for_clickable_element(driver, By.XPATH, next_button_xpath)
            print(f"Locating Next button: {next_button_xpath}")

        client = get_api(driver)
        if client:
            try:
                driver._asnb_submission_snapshot = client.snapshot(fund_name_variants(fund_name), amount)
            except (APISessionExpired, APIThrottled):
                print("API session restricted before submission; returning to eligibility checks without clicking.")
                return 'API_RECHECK'
            except APIReadError:
                print("Transaction-history baseline unavailable; uncertain responses will require browser review.")

        max_retries = 30 # Maximum attempts for the purchase click
        retry_count = 0

        while retry_count < max_retries:
            print(f"Attempt {retry_count + 1}/{max_retries}: {'API handoff experiment' if api_transport else 'Clicking Next button'}...")
            submission_attempted = True
            if api_transport:
                from .api_purchase import submit_api_and_restore
                from .handoff import HandoffError
                try:
                    outcome = submit_api_and_restore(driver, fund_name, amount, bank_name, referral_code)
                except HandoffError as error:
                    print(str(error))
                    return 'UNCERTAIN'
            else:
                outcome = submit_and_observe(driver, next_button, timeout=20)
            if outcome == "UNCERTAIN":
                save_debug_snapshot(driver, "Submission outcome uncertain; automatic replay paused", "purchase")
                return "UNCERTAIN"
            if outcome == "SUCCESS":
                print("Successfully reached final payment confirmation page.")

                return pause_for_payment(driver, fund_name, amount, email_config)

            else:
                submission_attempted = False  # Explicit server rejection permits later retry.
                print(f"Purchase response: {outcome}")
                if api_transport:
                    if outcome not in ('INSUFFICIENT', 'BLOCKED'):
                        return 'UNCERTAIN'
                else:
                    _, ok_button = wait_for_clickable_any(driver, [
                        ("Response OK", By.XPATH, "//button[normalize-space()='OK' or normalize-space()='Ok' or normalize-space()='Tutup' or normalize-space()='Close']"),
                    ], description="purchase response dismissal")
                    ok_button.click()
                if outcome == "BLOCKED":
                    return "BLOCKED"
                stats.record_insufficient()
                retry_count += 1
                retry_delay = random.uniform(3, 8)
                print(f"Waiting {retry_delay:.1f} seconds before retrying...")
                time.sleep(retry_delay)
                if not api_transport:
                    next_button = wait_for_clickable_element(driver, By.XPATH, next_button_xpath)

        # If loop finishes after max_retries
        print(f"Purchase failed after {max_retries} retries, likely due to persistent 'insufficient units' error.")
        return False

    except (TimeoutException, NoSuchElementException) as e:
        print(f"Error during purchase flow: Could not find expected element. {e}")
        return "UNCERTAIN" if submission_attempted else False
    except WebDriverException as e:
        print(f"WebDriver error during purchase: {e}")
        return "UNCERTAIN" if submission_attempted else False
    except Exception as e:
        print(f"An unexpected error occurred during purchase: {e}")
        return "UNCERTAIN" if submission_attempted else False

# --- Main Execution ---


def main(profile=None, purchase_transport=DEFAULT_PURCHASE_TRANSPORT,
         api_subscription_transport='http'):
    """Main function to run the automation.

    Args:
        profile: Optional profile name to override credentials from [Profile.<name>] section.
    """
    driver = None
    browser_health = None
    setup_logging(profile)
    try:
        label = f" [{profile}]" if profile else ""
        print(f"--- Starting MyASNB Buyer Automation{label} ---")
        # 1. Load Configuration
        print("Loading configuration...")
        config = load_config(profile=profile)

        # Profile is required for automation
        if not profile:
            available = get_profile_names(config)
            if available:
                print(f"Error: --profile is required. Available profiles: {', '.join(available)}")
            else:
                print("Error: No profiles found. Add a [Profile.xxx] section to config.ini")
            return

        prof = config[f'Profile.{profile}']
        username = prof['username']
        password = prof['password']
        security_phrase = prof['security_phrase']
        print(f"Using profile: {profile} (username: {username})")
        # Get the list of funds, amount, bank name, loop tries, and email config
        # Profile can override any of these settings
        prof_section = f'Profile.{profile}'

        funds_to_try = get_funds_list(config)
        prof_funds = config.get(prof_section, 'funds_to_try', fallback='').strip()
        if prof_funds:
            funds_to_try = [f.strip() for f in prof_funds.split(',') if f.strip()]

        loop_tries = int(config.get('Settings', 'loop_tries', fallback=0))
        amount_to_buy = config.get('Settings', 'purchase_amount', fallback=None)
        bank_to_use = config.get('Settings', 'bank_name', fallback='Public Bank').strip()
        session_refresh_interval = get_session_refresh_interval(config)

        amount_to_buy = config.get(prof_section, 'purchase_amount', fallback=amount_to_buy)
        bank_to_use = config.get(prof_section, 'bank_name', fallback=bank_to_use).strip()
        referral_code = config.get('Settings', 'referral_code', fallback='').strip()
        referral_code = config.get(prof_section, 'referral_code', fallback=referral_code).strip()
        loop_tries = int(config.get(prof_section, 'loop_tries', fallback=loop_tries))
        session_refresh_interval = int(config.get(prof_section, 'session_refresh_interval', fallback=session_refresh_interval))

        memory_limit = config.getint(prof_section, 'browser_memory_limit_mb',
            fallback=config.getint('Settings', 'browser_memory_limit_mb', fallback=3072))
        if session_refresh_interval < 1 or memory_limit < 256:
            raise ConfigError('session_refresh_interval must be >= 1 and browser_memory_limit_mb >= 256')
        browser_health = BrowserHealth(limit_mb=memory_limit)

        email_config = get_email_config(config)
        if email_config:
            profile_recipient = config.get(prof_section, 'recipient_email', fallback='').strip()
            if profile_recipient:
                email_config['recipient_emails'] = [e.strip() for e in profile_recipient.split(',') if e.strip()]
                print(f"Using profile-specific email recipients: {', '.join(email_config['recipient_emails'])}")

        loop_desc = "infinite" if loop_tries == 0 else str(loop_tries)
        referral_desc = referral_code if referral_code else "none"
        print(f"Configuration loaded. Funds: {funds_to_try}, Loops: {loop_desc}, Amount: {amount_to_buy}, Bank: {bank_to_use}, Referral: {referral_desc}, Session health check: {session_refresh_interval} attempts; browser RSS limit: {memory_limit} MiB")
        if email_config:
            recipient_emails = email_config.get('recipient_emails', [])
            if len(recipient_emails) > 1:
                print(f"Email notifications enabled for {len(recipient_emails)} recipients: {', '.join(recipient_emails)}")
            else:
                print(f"Email notifications enabled for: {recipient_emails[0] if recipient_emails else 'Unknown'}")
        else:
            print("Email notifications disabled.")

        if not funds_to_try:
            print("No funds specified in 'funds_to_try' setting in config.ini. Exiting.")
            return
        if not amount_to_buy:
            print("No purchase amount specified in 'purchase_amount' setting in config.ini. Exiting.")
            return

        # Validate bank name against known options
        VALID_BANKS = [
            "Affin Bank", "Alliance Bank", "AmBank", "Bank Islam", "Bank Rakyat",
            "Bank Muamalat", "CIMB Clicks", "Hong Leong Bank", "HSBC Bank",
            "Maybank2U", "Public Bank", "RHB Bank", "UOB Bank", "BSN", "KFH",
            "Maybank2E", "OCBC Bank", "Standard Chartered", "AGRONet", "Bank of China"
        ]
        if bank_to_use not in VALID_BANKS:
            print(f"WARNING: Bank '{bank_to_use}' not in known list: {VALID_BANKS}")
            print("This may cause bank selection to fail. Check spelling in config.ini.")

        tracing_enabled = config.getboolean(prof_section, 'network_trace',
            fallback=config.getboolean('Settings', 'network_trace', fallback=True))
        lean_assets = config.getboolean(prof_section, 'lean_assets',
            fallback=config.getboolean('Settings', 'lean_assets', fallback=True))
        page_load_strategy = config.get(prof_section, 'page_load_strategy',
            fallback=config.get('Settings', 'page_load_strategy', fallback='eager')).strip()
        if page_load_strategy not in ('normal', 'eager'):
            raise ConfigError('page_load_strategy must be normal or eager')
        print(f"Browser loading: {page_load_strategy}; decorative banner blocking: {lean_assets}; cache enabled.")
        api_reads = config.getboolean(prof_section, 'api_reads',
            fallback=config.getboolean('Settings', 'api_reads', fallback=True))
        print(f"Read-only API preflight and reconciliation: {api_reads}")
        print(f"Purchase transport: {purchase_transport} (final agreement and FPX payment remain manual)")
        trace_name = hashlib.sha256(profile.encode()).hexdigest()[:12]
        trace_path = Path('network_traces') / f'{trace_name}_{os.getpid()}.jsonl' if tracing_enabled else None
        if trace_path:
            print(f"Passive network trace: {trace_path} (redacted metadata, rotating files)")
        def new_browser():
            browser = get_webdriver("chrome", trace_path=trace_path, lean_assets=lean_assets,
                                    page_load_strategy=page_load_strategy)
            browser._asnb_purchase_transport = purchase_transport
            if (api_reads or purchase_transport == 'api') and isinstance(browser.capabilities, dict):
                browser._asnb_api = PortalAPI(browser)
            return browser

        if purchase_transport == 'api':
            from .api_runner import run_http_runner
            purchase_timeout = config.getint(prof_section, 'api_purchase_timeout_seconds',
                fallback=config.getint('Settings', 'api_purchase_timeout_seconds', fallback=60))
            if not 20 <= purchase_timeout <= 180:
                raise ConfigError('api_purchase_timeout_seconds must be between 20 and 180')
            run_http_runner(username, password, funds_to_try, amount_to_buy, bank_to_use,
                            referral_code, loop_tries, session_refresh_interval, email_config,
                            new_browser, sys.modules[__name__], purchase_timeout=purchase_timeout,
                            subscription_transport=api_subscription_transport)
            return

        # 2. Initialize WebDriver
        print("Initializing WebDriver...")
        driver = new_browser() # Or get browser from config

        # 3. Login with retry for active session conflicts
        max_login_retries = 3
        login_successful = False
        
        for login_attempt in range(1, max_login_retries + 1):
            print(f"Login attempt {login_attempt}/{max_login_retries}...")
            
            if login(driver, username, password, security_phrase):
                login_successful = True
                print("✅ Login successful!")
                break
            else:
                if login_attempt < max_login_retries:
                    print(f"❌ Login attempt {login_attempt} failed. Retrying...")
                    # Brief pause before retry (active session handler already includes its own wait)
                    time.sleep(5)
                else:
                    print(f"❌ All {max_login_retries} login attempts failed.")
        
        if not login_successful:
            print("Login failed after all retry attempts. Exiting.")
            return

        def maintain_browser(cooldown_seconds):
            nonlocal driver
            trace_network(driver)
            rss = browser_health.sample(driver)
            if rss is not None:
                print(f"Browser health: owned process-tree RSS {rss:.0f} MiB (limit {memory_limit} MiB; shared pages may be counted twice).")
                print(f"Network health (current browser): {network_summary(driver)}")
                if get_api(driver):
                    print(f"Read-only API health: {get_api(driver).metrics}")
            if not browser_health.restart_due(cooldown_seconds):
                return True
            print("Sustained browser memory limit exceeded. Restarting during cooldown." if cooldown_seconds >= 45
                  else "Sustained browser memory limit exceeded; maximum deferral reached. Restarting.")
            try:
                logout(driver)
            except WebDriverException as error:
                print(f"Pre-maintenance logout failed: {error}")
            close_webdriver(driver)
            driver = new_browser()
            browser_health.reset()
            stats.record_refresh()
            for attempt in range(3):
                if login(driver, username, password, security_phrase):
                    return True
                if attempt < 2:
                    time.sleep(5)
            print("Browser maintenance re-login failed. Restarting automation.")
            return False

        # 4. Loop through funds in round-robin (infinite if loop_tries=0, else limited)
        import itertools
        purchase_successful = False
        fund_attempt_count = 0
        max_attempts = loop_tries * len(funds_to_try) if loop_tries > 0 else 0  # 0 = no limit
        fund_iterator = itertools.cycle(funds_to_try)

        for current_fund in fund_iterator:
            fund_attempt_count += 1

            # Check if we've exceeded max attempts (0 = infinite)
            if max_attempts > 0 and fund_attempt_count > max_attempts:
                print(f"Reached maximum {max_attempts} attempts ({loop_tries} loops). Stopping.")
                break

            all_on_cooldown = all(fund_cooldowns.is_on_cooldown(f) for f in funds_to_try)
            idle_window = min(fund_cooldowns.remaining_cooldown(f) for f in funds_to_try) if all_on_cooldown else 0
            if not maintain_browser(idle_window):
                return

            # Skip funds on block cooldown
            if fund_cooldowns.is_on_cooldown(current_fund):
                remaining = fund_cooldowns.remaining_cooldown(current_fund)
                fund_attempt_count -= 1  # Don't count skips as attempts

                # Check if ALL funds are on cooldown - sleep until first one expires
                all_on_cooldown = all(fund_cooldowns.is_on_cooldown(f) for f in funds_to_try)
                if all_on_cooldown:
                    min_wait = min(fund_cooldowns.remaining_cooldown(f) for f in funds_to_try)
                    print(f"All funds on cooldown. Sleeping {min_wait:.0f}s until next fund available...")
                    send_desktop_notification("ASNB - All Blocked", f"All funds blocked. Sleeping {min_wait:.0f}s", sound=False)
                    # Recompute after maintenance; logout/login consumes part of the cooldown.
                    time.sleep(min(60, min_wait + 0.25))
                else:
                    # Just skip this fund silently and try next
                    pass
                continue

            api_result = api_preflight(driver, current_fund, funds_to_try, bank_to_use)
            if api_result == 'SKIP':
                continue
            if api_result == 'SESSION_EXPIRED':
                if not (logout(driver) and login(driver, username, password, security_phrase)):
                    return
                continue
            if purchase_transport != 'api' and api_result == 'READY' and getattr(driver, '_asnb_portfolio_refresh_needed', False) is True:
                if not refresh_portfolio_ui(driver, current_fund):
                    continue

            # Send periodic summary email
            maybe_send_summary_email(email_config)

            stats.record_attempt(current_fund)
            print(f"\n--- Attempting purchase for fund: {current_fund} (Attempt {fund_attempt_count}) ---")
            
            # Check portfolio health instead of logging out on a fixed attempt count.
            if fund_attempt_count > 1 and (fund_attempt_count - 1) % session_refresh_interval == 0:
                try:
                    client = get_api(driver)
                    if client and api_result == 'READY':
                        print("API session health verified by fresh preflight.")
                    else:
                        WebDriverWait(driver, 2, poll_frequency=0.25).until(
                            lambda d: d.current_url == MYASNB_PORTFOLIO_URL and
                            all(find_fund_action(d, fund_name_variants(f)) for f in funds_to_try))
                    print("Session health check passed; keeping current login.")
                except (TimeoutException, WebDriverException, APIReadError):
                    print("Session health check failed. Refreshing login.")
                    stats.record_refresh()
                    if not (logout(driver) and login(driver, username, password, security_phrase)):
                        print("Session recovery failed. Restarting automation.")
                        return

            # 4a. Navigate to Purchase Section for the current fund
            nav_result = True if purchase_transport == 'api' else navigate_to_purchase(driver, current_fund)

            if nav_result == "INSUFFICIENT":
                stats.record_insufficient()
                print(f"No units available for {current_fund}; rotating without a block cooldown.")
                if not navigate_to_portfolio(driver):
                    return
                time.sleep(random.uniform(3, 8))
                continue

            if nav_result == "BLOCKED":
                mark_fund_ui_stale(driver, current_fund)
                print(f"Fund {current_fund} blocked at navigation stage. Applying cooldown.")
                stats.record_block(current_fund)
                fund_cooldowns.mark_blocked(current_fund, getattr(driver, '_asnb_block_retry_seconds', 300))
                if not navigate_to_portfolio(driver):
                    if logout(driver) and login(driver, username, password, security_phrase):
                        print("Session reset after nav block.")
                    else:
                        print("Session reset failed. Exiting.")
                        return
                continue

            if not nav_result:
                print(f"Navigation to purchase section failed for {current_fund}.")
                
                # Try to recover by navigating back to portfolio
                if not navigate_to_portfolio(driver):
                    print("Failed to navigate back to portfolio. Attempting logout and re-login to reset session...")
                    
                    # Logout and re-login to reset session state
                    if logout(driver):
                        print("Logout successful. Attempting to log back in...")
                        if login(driver, username, password, security_phrase):
                            print("Re-login successful. Continuing with next fund...")
                            continue  # Try the next fund after successful re-login
                        else:
                            print("Re-login failed after logout. Exiting.")
                            return
                    else:
                        print("Logout failed. Unable to recover session. Exiting.")
                        return
                else:
                    # Verify that funds are properly loaded after navigation
                    print("Successfully navigated back to portfolio. Verifying fund loading...")
                    if not verify_funds_loaded(driver, funds_to_try):
                        print("⚠️ Funds not properly loaded. Attempting session refresh...")
                        if logout(driver) and login(driver, username, password, security_phrase):
                            print("✅ Session refresh successful after fund loading issue.")
                        else:
                            print("❌ Session refresh failed. Continuing anyway...")
                    
                    continue # Try the next fund

            # 4b. Attempt Purchase Unit, passing the bank name
            purchase_status = purchase_unit(driver, current_fund, amount_to_buy, bank_to_use, email_config, referral_code)

            if purchase_status == "API_RECHECK":
                if not navigate_to_portfolio(driver):
                    return
                continue

            if purchase_status == "UNCERTAIN":
                reconcile_submission(driver)
                wait_for_submission_review(driver)
                if not navigate_to_portfolio(driver):
                    return
                continue

            if purchase_status == "RESUME":
                print(f"Purchase completed for {current_fund}. Resuming with remaining funds...")
                purchase_successful = True
                # Full Chrome restart - browser state is unpredictable after FPX payment redirect
                print("Restarting Chrome for clean session after payment...")
                try:
                    print("Logging out before Chrome restart...")
                    logout(driver)
                except Exception as logout_err:
                    print(f"Pre-restart logout failed: {logout_err}")
                try:
                    close_webdriver(driver)
                except Exception:
                    pass
                driver = new_browser()
                browser_health.reset()
                if login(driver, username, password, security_phrase):
                    print("Fresh session ready. Continuing with next fund...")
                else:
                    print("Re-login failed after Chrome restart. Will retry via outer loop.")
                    break
                continue
            elif purchase_status is True:
                print(f"Purchase successfully initiated and confirmed for {current_fund}.")
                purchase_successful = True
                break # Exit the loop on success (user chose Ctrl+C)
            elif purchase_status == "BLOCKED":
                mark_fund_ui_stale(driver, current_fund)
                stats.record_block(current_fund)
                fund_cooldowns.mark_blocked(current_fund, getattr(driver, '_asnb_block_retry_seconds', 300))
                print(f"Purchase attempt for {current_fund} was blocked (1001). Cooldown {fund_cooldowns.remaining_cooldown(current_fund):.0f}s. Trying next fund.")
                # Navigate back to portfolio page before trying the next fund
                if not navigate_to_portfolio(driver):
                    print("Failed to navigate back to portfolio after 'Blocked' error. Attempting session reset...")
                    if logout(driver) and login(driver, username, password, security_phrase):
                        print("Session reset successful. Continuing with next fund...")
                    else:
                        print("Session reset failed. Exiting.")
                        return
                else:
                    # Verify fund loading after blocked error recovery
                    if not verify_funds_loaded(driver, funds_to_try):
                        print("⚠️ Funds not properly loaded after blocked error. Refreshing session...")
                        if logout(driver) and login(driver, username, password, security_phrase):
                            print("✅ Session refresh successful after blocked error fund loading issue.")
                        else:
                            print("❌ Session refresh failed. Continuing anyway...")
                continue # Try the next fund
            else: # purchase_status is False
                print(f"Purchase initiation failed for {current_fund} due to other errors or max retries.")
                # Navigate back to portfolio before trying next fund
                if not navigate_to_portfolio(driver):
                    print("Failed to navigate back to portfolio after purchase failure. Attempting session reset...")
                    if logout(driver) and login(driver, username, password, security_phrase):
                        print("Session reset successful. Continuing with next fund...")
                    else:
                        print("Session reset failed. Exiting.")
                        return
                else:
                    # Verify fund loading after purchase failure recovery
                    if not verify_funds_loaded(driver, funds_to_try):
                        print("⚠️ Funds not properly loaded after purchase failure. Refreshing session...")
                        if logout(driver) and login(driver, username, password, security_phrase):
                            print("✅ Session refresh successful after purchase failure fund loading issue.")
                        else:
                            print("❌ Session refresh failed. Continuing anyway...")
                continue # Try the next fund

        # 5. Final Status Report
        if purchase_successful:
            print("\n--- Overall Result: Purchase successful for one of the funds. ---")
        else:
            print("\n--- Overall Result: Purchase failed for all specified funds. ---")
        print(stats.summary())

    except ConfigError as e:
        print(f"Configuration Error: {e}")
    except DriverSetupError as e:
        print(f"Driver Setup Error: {e}")
    except KeyboardInterrupt:
        print("\n--- Runner stopping. Attempting graceful shutdown... ---")
        print(stats.summary())
        if driver:
            try:
                print("Attempting logout before exit...")
                if not logout(driver):
                    print("Logout failed during shutdown.")
            except Exception as shutdown_err:
                print(f"Logout error during shutdown: {shutdown_err}")
        raise  # Re-raise to exit the while loop
    except Exception as e:
        print(f"An unexpected error occurred in the main process: {e}")
    finally:
        if driver:
            print("Closing WebDriver...")
            try:
                close_webdriver(driver)
                print("WebDriver closed.")
            except Exception as close_error:
                print(f"WebDriver cleanup failed: {close_error}")
        print("--- Automation finished ---")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ASNB Buyer Automation")
    parser.add_argument("--profile", type=str, default=None,
                        help="Profile name to use from [Profile.<name>] section in config.ini")
    parser.add_argument('--purchase-transport', choices=('ui', 'api'), default=DEFAULT_PURCHASE_TRANSPORT,
                        help='Purchase submission transport (default: api); final payment stays manual')
    parser.add_argument('--api-subscription-transport', choices=('http', 'chrome'), default='http',
                        help='http: initialized HTTP (default); chrome: keep Chrome for API fetch')
    args = parser.parse_args()

    # Handle SIGTERM (from GUI Stop button) same as Ctrl+C
    _sigterm_received = False
    def _handle_sigterm(signum, frame):
        global _sigterm_received
        _sigterm_received = True
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Watchdog: auto-exit if parent process (GUI) dies
    # When parent dies, PPID changes to 1 (launchd on macOS) or init on Linux
    _initial_ppid = os.getppid()
    def _parent_watchdog():
        while True:
            time.sleep(2)
            if os.getppid() != _initial_ppid:
                print("\n--- Parent process died. Shutting down... ---")
                sys.stdout.flush()
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)
                os._exit(1)
    _watchdog = threading.Thread(target=_parent_watchdog, daemon=True)
    _watchdog.start()

    while True:
        try:
            main(profile=args.profile, purchase_transport=args.purchase_transport,
                 api_subscription_transport=args.api_subscription_transport)
        except KeyboardInterrupt:
            print("\n--- Script stopped (Ctrl+C or SIGTERM). ---")
            break
        except Exception:
            pass

        if _sigterm_received:
            break

        print("\n--- Restarting automation process in 3 seconds... ---")
        time.sleep(3)
