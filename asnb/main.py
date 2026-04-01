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
LOG_FILE = LOG_DIR / f"asnb_buyer_{datetime.now().strftime('%Y%m%d')}.log"

# Configure logging to both console and file
logger = logging.getLogger("asnb_buyer")
logger.setLevel(logging.INFO)

# File handler - append mode, one log file per day
file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(file_handler)

# Console handler - flush after every message so GUI pipe receives output immediately
class _FlushingStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

console_handler = _FlushingStreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(console_handler)

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

# --- macOS Desktop Notification ---
def send_desktop_notification(title, message, sound=True):
    """Send a native macOS notification banner."""
    if platform.system() != "Darwin":
        return
    try:
        sound_flag = 'with sound "Glass"' if sound else ""
        escaped_msg = message.replace('"', '\\"').replace("'", "\\'")
        escaped_title = title.replace('"', '\\"')
        os.system(f'''osascript -e 'display notification "{escaped_msg}" with title "{escaped_title}" {sound_flag}' ''')
    except Exception as e:
        print(f"Desktop notification failed: {e}")

# --- Block Cooldown Tracking ---
class FundCooldownTracker:
    """Skip recently-blocked funds for a cooldown period to avoid wasting attempts."""
    COOLDOWN_SECONDS = 120  # Skip blocked funds for 2 minutes

    def __init__(self):
        self._blocked_at = {}  # fund_name -> datetime

    def mark_blocked(self, fund_name):
        self._blocked_at[fund_name] = datetime.now()

    def is_on_cooldown(self, fund_name):
        if fund_name not in self._blocked_at:
            return False
        elapsed = (datetime.now() - self._blocked_at[fund_name]).total_seconds()
        if elapsed >= self.COOLDOWN_SECONDS:
            del self._blocked_at[fund_name]
            return False
        return True

    def remaining_cooldown(self, fund_name):
        if fund_name not in self._blocked_at:
            return 0
        elapsed = (datetime.now() - self._blocked_at[fund_name]).total_seconds()
        return max(0, self.COOLDOWN_SECONDS - elapsed)

fund_cooldowns = FundCooldownTracker()

# --- Periodic Summary Notification ---
SUMMARY_INTERVAL_SECONDS = 3600  # Send summary every hour
_last_summary_time = datetime.now()

def maybe_send_summary_email(email_config):
    """Send periodic summary email if enough time has passed."""
    global _last_summary_time
    elapsed = (datetime.now() - _last_summary_time).total_seconds()
    if elapsed < SUMMARY_INTERVAL_SECONDS or not email_config:
        return
    _last_summary_time = datetime.now()
    try:
        summary_text = stats.summary()
        # Use a copy with send_on_failure forced true so periodic summaries always send
        summary_config = dict(email_config)
        summary_config['send_on_failure'] = True
        send_purchase_notification(
            email_config=summary_config,
            success=False,
            fund_name="Periodic Status Update",
            amount="N/A",
            error_message=f"Script is still running.{summary_text}"
        )
        print("Periodic summary email sent.")
    except Exception as e:
        print(f"Failed to send summary email: {e}")

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.common.exceptions import WebDriverException, TimeoutException, NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

try:
    from asnb.config import load_config, get_funds_list, get_email_config, get_session_refresh_interval, get_profile_names, ConfigError
    from asnb.driver import get_webdriver, DriverSetupError
    from asnb.email import send_purchase_notification, EmailNotificationError
    from asnb.actions import human_type, human_click, human_js_click, human_delay, human_scroll_to, between_actions, after_page_load
except ImportError as e:
    print(f"Error importing modules: {e}")
    print("Ensure the asnb package is on the Python path (run with python -m asnb.main).")
    sys.exit(1)

# --- Constants ---
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

def login(driver: WebDriver, username: str, password: str, security_phrase: str) -> bool: # security_phrase kept for now, though not used in user's flow
    """
    Attempts to log into the MyASNB website.

    Args:
        driver: The initialized Selenium WebDriver instance.
        username: The user's MyASNB username.
        password: The user's MyASNB password.
        security_phrase: The user's MyASNB security phrase.

    Returns:
        True if login is successful (or appears successful), False otherwise.
    """
    """
    Attempts to log into the MyASNB website using the specified flow.

    Args:
        driver: The initialized Selenium WebDriver instance.
        username: The user's MyASNB username.
        password: The user's MyASNB password.
        security_phrase: The user's MyASNB security phrase (currently unused in this flow).

    Returns:
        True if login is successful, False otherwise.
    """
    print("Attempting to log in...")
    try:
        # 1. Navigate to Login Page
        driver.get(MYASNB_LOGIN_URL)
        print(f"Navigated to {MYASNB_LOGIN_URL}")

        # 2. Handle Optional Popup (Robust Multi-Selector Approach)
        print("Checking for login page popup overlay...")
        overlay_xpath = "//div[contains(@class, 'opacity-25 fixed inset-0 z-40 bg-black')]"
        
        # Multiple selector strategies for popup close button (tested and verified)
        popup_close_selectors = [
            ("CSS - SVG positioned (primary)", By.CSS_SELECTOR, "svg.absolute.top-1.right-1"),
            ("CSS - SVG in rounded container", By.CSS_SELECTOR, "div.rounded-2xl svg"),
            ("XPath - Positioned SVG", By.XPATH, "//svg[contains(@class, 'absolute') and contains(@class, 'cursor-pointer')]"),
        ]
        
        try:
            # Wait briefly for the overlay to appear (if it exists)
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, overlay_xpath)))
            print("Popup overlay detected. Attempting to find and click close button...")

            popup_close_element = None
            successful_selector = None
            
            # Try each selector in order
            for selector_name, by_method, selector in popup_close_selectors:
                try:
                    print(f"Trying {selector_name}: {selector}")
                    popup_close_element = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((by_method, selector))
                    )
                    successful_selector = selector_name
                    print(f"✓ Found clickable close button using: {selector_name}")
                    break
                except TimeoutException:
                    print(f"✗ {selector_name} not found, trying next...")
                    continue
            
            if not popup_close_element:
                print("Warning: Could not find popup close button with any selector")
                raise TimeoutException("No working popup close selector found")

            # Use proper event dispatch for SVG elements (tested and verified working)
            try:
                print("Attempting JavaScript event dispatch on SVG close button...")
                driver.execute_script("""
                    const clickEvent = new MouseEvent('click', {
                        view: window,
                        bubbles: true,
                        cancelable: true
                    });
                    arguments[0].dispatchEvent(clickEvent);
                """, popup_close_element)
                print("JavaScript event dispatch executed successfully.")
            except Exception as js_click_err:
                print(f"JavaScript event dispatch failed ({js_click_err}), trying direct click fallback...")
                try:
                    popup_close_element.click()
                    print("Direct click executed successfully.")
                except Exception as direct_click_err:
                     print(f"Direct click also failed ({direct_click_err}). Popup might remain.")
                     raise TimeoutException("Failed to click popup close button with any method.")

            # Wait for the overlay to disappear
            print("Waiting for popup overlay to disappear...")
            WebDriverWait(driver, WAIT_TIMEOUT).until(
                EC.invisibility_of_element_located((By.XPATH, overlay_xpath))
            )
            print("Popup overlay disappeared. Proceeding with login.")

        except TimeoutException:
            # This means either the overlay didn't appear initially, or it didn't disappear after clicking close
            print("Popup overlay not detected initially, or did not disappear after attempting close. Assuming no active popup blocking interaction.")
        except Exception as popup_err:
            print(f"An unexpected error occurred while handling the popup: {popup_err}")
            # Decide if you want to proceed or stop if popup handling fails unexpectedly

        # 3. Enter Username (human-like typing)
        print("Entering username...")
        username_field = wait_for_element(driver, By.NAME, "username")
        human_type(username_field, username)
        print("Username entered.")
        between_actions()

        # 4. Click First Continue Button (Teruskan)
        print("Clicking 'Teruskan' button...")
        continue_button_1 = wait_for_clickable_element(driver, By.ID, "no-printable")
        human_click(driver, continue_button_1)
        print("'Teruskan' button clicked.")

        # 5. Click "Yes" Button
        print("Waiting for and clicking 'Yes' button...")
        yes_button = wait_for_clickable_element(driver, By.ID, "btnYes")
        human_click(driver, yes_button)
        print("'Yes' button clicked.")
        between_actions()

        # 6. Enter Password (human-like typing)
        print("Entering password...")
        password_field = wait_for_element(driver, By.NAME, "password")
        human_type(password_field, password)
        print("Password entered.")
        between_actions()

        # 7. Click Login Button
        print("Clicking 'Login'...")
        try:
            login_button = wait_for_clickable_element(driver, By.XPATH, "//button[.//span[text()='Log Masuk']]", timeout=WAIT_TIMEOUT)
        except TimeoutException:
            login_button = wait_for_clickable_element(driver, By.XPATH, "//button[.//span[text()='Login']]", timeout=WAIT_TIMEOUT)
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
                print(f"⚠️ Active session detected: {error_text}")
                print("Waiting 90 seconds for session to clear (actual wait time is usually ~1 minute)...")
                
                # Wait 90 seconds with progress indication
                for remaining in range(90, 0, -10):
                    print(f"⏳ Waiting... {remaining} seconds remaining")
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

def navigate_to_purchase(driver: WebDriver, fund_to_buy: Optional[str]) -> bool:
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
        # Use the fund_to_buy variable passed into the function
        try:
            add_invest_xpath = f"(//div[./div/span[contains(text(), '{fund_to_buy}')] and .//span[contains(text(), 'Pelaburan Tambahan')]]//span[contains(text(), 'Pelaburan Tambahan')])[1]"
            add_invest_span = wait_for_element(driver, By.XPATH, add_invest_xpath, timeout=WAIT_TIMEOUT * 2) # Longer timeout for dashboard elements
        except:
            add_invest_xpath = f"(//div[./div/span[contains(text(), '{fund_to_buy}')] and .//span[contains(text(), 'Add Invest')]]//span[contains(text(), 'Add Invest')])[1]"
            add_invest_span = wait_for_element(driver, By.XPATH, add_invest_xpath, timeout=WAIT_TIMEOUT * 2) # Longer timeout for dashboard elements
        print(f"Waiting for the presence of the 'Add Invest' span for '{fund_to_buy}' using parent-based XPath: {add_invest_xpath}")

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
            wait_for_element(driver, By.XPATH, amount_input_xpath, timeout=WAIT_TIMEOUT * 2)
            print("Successfully navigated to purchase page (amount input found).")
            return True
        except TimeoutException:
            print("Amount input not found. Checking for block popup at navigation stage...")

            # Check if a block/error popup appeared instead of the purchase page
            block_xpaths = [
                "//p[contains(text(), 'Blocked due to retry') or contains(text(), '(1001)')]",
                "//p[contains(text(), 'blocked') or contains(text(), 'Blocked')]",
                "//p[contains(text(), 'insufficient units available')]",
            ]
            for bx in block_xpaths:
                try:
                    wait_for_element(driver, By.XPATH, bx, timeout=2)
                    print(f"Block/error popup detected at navigation stage!")
                    # Try to dismiss it
                    try:
                        ok_btn = wait_for_clickable_element(driver, By.XPATH, "//button[contains(text(), 'OK')]", timeout=3)
                        ok_btn.click()
                        print("Dismissed block popup OK button.")
                        between_actions()
                    except TimeoutException:
                        print("No OK button found on block popup.")
                    return "BLOCKED"
                except TimeoutException:
                    continue

            print("No block popup found either. Navigation failed for unknown reason.")
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
                fund_xpath = f"//span[contains(text(), '{fund_name}')]"
                fund_elements = driver.find_elements(By.XPATH, fund_xpath)
                if fund_elements:
                    funds_found += 1
                    print(f"✓ Found fund: {fund_name}")
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
        
        my_account_element = None
        successful_selector = None
        
        for selector_name, by_method, selector in my_account_selectors:
            try:
                print(f"Trying {selector_name}: {selector}")
                my_account_element = wait_for_clickable_element(driver, by_method, selector, timeout=WAIT_TIMEOUT)
                successful_selector = selector_name
                print(f"✓ Found clickable 'My Account' element using: {selector_name}")
                break
            except TimeoutException:
                print(f"✗ {selector_name} not found, trying next...")
                continue
        
        if not my_account_element:
            print("Warning: Could not find 'My Account' link with any selector")
            return False

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
        
        print("Successfully navigated back to portfolio page.")
        time.sleep(3) # Pause after navigation
        return True
        
    except (TimeoutException, NoSuchElementException, WebDriverException) as e:
        print(f"Error navigating back to portfolio: {e}")
        return False


# --- Modified Purchase Unit Function ---
def purchase_unit(driver: WebDriver, fund_name: Optional[str], amount: Optional[str], bank_name: str, email_config: dict = None) -> Union[bool, str]:
    """
    Executes the purchase of units for a specified fund. Handles retry logic for insufficient units
    and detects the 'Blocked' error. Uses the specified bank name.

    Args:
        driver: The initialized Selenium WebDriver instance.
        fund_name: The name of the fund being purchased (for logging).
        amount: The amount to invest.

    Returns:
        True: If the purchase reaches the final confirmation page.
        "RESUME": If the purchase succeeded and user wants to continue with remaining funds.
        "BLOCKED": If the 'Blocked due to retry' (1001) error occurs.
        False: If any other error occurs or retries are exhausted.
    """
    print(f"Attempting to purchase units...")
    # Amount should be loaded from config in main() and passed here
    if not amount:
        print("Purchase amount not provided. Skipping purchase.")
        return False # Amount is required

    # fund_name is determined by the navigation step, not needed here unless verifying
    print(f"Amount to invest: {amount}")
    try:
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

        # 3. Wait for and click agreement checkbox
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


        # 4. Locate the Next button
        try:
            next_button_xpath = "//button[@type='submit' and contains(., 'Seterusnya')]"
            next_button = wait_for_clickable_element(driver, By.XPATH, next_button_xpath)
        except:
            next_button_xpath = "//button[@type='submit' and contains(., 'Next')]"
            next_button = wait_for_clickable_element(driver, By.XPATH, next_button_xpath)
        print(f"Locating Next button: {next_button_xpath}")

        max_retries = 30 # Maximum attempts for the purchase click
        retry_count = 0

        while retry_count < max_retries:
            print(f"Attempt {retry_count + 1}/{max_retries}: Clicking Next button...")
            try:
                next_button.click()
                print("Next button clicked.")
            except WebDriverException as click_err:
                print(f"Error clicking Next button: {click_err}. Attempting to re-locate and retry.")
                time.sleep(2) # Brief pause before re-locating
                try:
                    next_button = wait_for_clickable_element(driver, By.XPATH, next_button_xpath)
                    next_button.click()
                    print("Next button clicked after re-locating.")
                except Exception as relocate_click_err:
                    print(f"Failed to click Next button even after re-locating: {relocate_click_err}")
                    return False # Abort if clicking fails persistently


            # Wait a bit for the page to react after clicking Next
            time.sleep(3) # Adjust as needed

            # --- Check for SUCCESS condition (Final Confirmation Page) ---
            final_checkbox_xpath = "//input[@type='checkbox' and @value='agree']"
            final_button_xpath = "//button[contains(., 'Teruskan Pembayaran') or contains(., 'Pembayaran') or contains(., 'Payment')]"
            try:
                print("Checking for final confirmation page elements...")
                # Use shorter timeouts for checks, maybe slightly longer for page load
                wait_for_element(driver, By.XPATH, final_checkbox_xpath, timeout=7)
                wait_for_element(driver, By.XPATH, final_button_xpath, timeout=7)
                print("Successfully reached final payment confirmation page.")

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

                # Send email notification if configured
                if email_config:
                    try:
                        send_purchase_notification(
                            email_config=email_config,
                            success=True,
                            fund_name=fund_name,
                            amount=amount,
                            screenshot_paths=screenshot_paths
                        )
                    except Exception as email_err:
                        print(f"Warning: Failed to send email notification: {email_err}")

                # Play platform-specific success notification + desktop banner
                success_message = f"Success! You have RM{amount} amount of {fund_name} units available for purchase. Please complete the payment."
                print(f"\n*** {success_message} ***\n")
                send_desktop_notification("ASNB Purchase Ready!", f"RM{amount} of {fund_name} - Complete payment now!", sound=True)
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
                    if _resume_eof.is_set():
                        print("\n--- Stdin closed (GUI exited?). Stopping. ---")
                        return True
                    print("Resuming automation after payment...")
                    return "RESUME"
                except (KeyboardInterrupt, EOFError):
                    print("\n--- User chose to stop. ---")
                    return True

            except TimeoutException:
                    print("Final confirmation page not detected yet.")

                    # --- Check for INSUFFICIENT UNITS error popup (068) ---
                    insufficient_units_text_xpath = "//p[contains(text(), 'insufficient units available') and contains(text(), '(068)')]"
                    insufficient_units_ok_xpath = "//div[.//p[contains(text(), 'insufficient units available')]]//button[contains(text(), 'OK')]"
                    try:
                        print("Checking for 'insufficient units' (068) error popup...")
                        wait_for_element(driver, By.XPATH, insufficient_units_text_xpath, timeout=5)
                        print("'Insufficient units' (068) error popup detected.")
                        print(f"Waiting for OK button: {insufficient_units_ok_xpath}")
                        error_ok_button = wait_for_clickable_element(driver, By.XPATH, insufficient_units_ok_xpath)
                        print("Clicking OK button...")
                        error_ok_button.click()
                        print("OK button clicked.")
                        stats.record_insufficient()
                        retry_delay = random.uniform(3, 8)
                        print(f"Waiting {retry_delay:.1f} seconds before retrying...")
                        time.sleep(retry_delay)
                        retry_count += 1
                        print("Re-locating Next button for retry...")
                        next_button = wait_for_clickable_element(driver, By.XPATH, next_button_xpath)
                        continue # Retry clicking Next

                    except TimeoutException:
                        print("'Insufficient units' (068) error not found.")

                        # --- Check for BLOCKED DUE TO RETRY error popup (1001) ---
                        blocked_text_xpath = "//p[contains(text(), 'Blocked due to retry') or contains(text(), '(1001)')]"
                        # Use same parent-agnostic pattern as the 068 OK button (which works reliably)
                        blocked_ok_xpath = "//div[.//p[contains(text(), 'Blocked due to retry') or contains(text(), '(1001)')]]//button[contains(text(), 'OK')]"
                        try:
                            print("Checking for 'Blocked due to retry' (1001) error popup...")
                            wait_for_element(driver, By.XPATH, blocked_text_xpath, timeout=5)
                            print("'Blocked due to retry' (1001) error popup detected.")
                            print(f"Waiting for OK button: {blocked_ok_xpath}")
                            try:
                                blocked_ok_button = wait_for_clickable_element(driver, By.XPATH, blocked_ok_xpath, timeout=5)
                            except TimeoutException:
                                # Fallback: try any visible OK button on the page
                                print("Primary OK button XPath failed, trying fallback...")
                                blocked_ok_button = wait_for_clickable_element(driver, By.XPATH, "//button[contains(text(), 'OK')]", timeout=5)
                            print("Clicking OK button...")
                            blocked_ok_button.click()
                            print("OK button clicked.")
                            print("Returning 'BLOCKED' status to switch funds.")
                            return "BLOCKED" # Special status to indicate this specific error

                        except TimeoutException:
                            # Neither success nor any known error popup was found
                            print("'Blocked due to retry' (1001) error not found.")
                            print("Did not find final confirmation page OR any known error popup.")
                            print("Assuming an unexpected state or different error. Aborting purchase attempt for this fund.")
                            # Screenshot for debugging unexpected states
                            try:
                                debug_screenshot = f"asnb_debug_{fund_name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                                driver.save_screenshot(debug_screenshot)
                                print(f"Debug screenshot saved: {debug_screenshot}")
                            except Exception:
                                pass
                            return False

                    except Exception as e:
                        print(f"An unexpected error occurred while handling popups: {e}")
                        try:
                            debug_screenshot = f"asnb_error_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                            driver.save_screenshot(debug_screenshot)
                            print(f"Error screenshot saved: {debug_screenshot}")
                        except Exception:
                            pass
                        return False

        # If loop finishes after max_retries
        print(f"Purchase failed after {max_retries} retries, likely due to persistent 'insufficient units' error.")
        return False

    except (TimeoutException, NoSuchElementException) as e:
        print(f"Error during purchase setup (before clicking Next): Could not find expected element. {e}")
        return False
    except WebDriverException as e:
        print(f"WebDriver error during purchase: {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during purchase: {e}")
        return False

# --- Main Execution ---
# Chrome restart interval: restart Chrome every N session refreshes to prevent memory leaks
CHROME_RESTART_INTERVAL = 3

def main(profile=None):
    """Main function to run the automation.

    Args:
        profile: Optional profile name to override credentials from [Profile.<name>] section.
    """
    driver = None
    chrome_restart_count = 0
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
        loop_tries = int(config.get(prof_section, 'loop_tries', fallback=loop_tries))
        session_refresh_interval = int(config.get(prof_section, 'session_refresh_interval', fallback=session_refresh_interval))

        email_config = get_email_config(config)
        if email_config:
            profile_recipient = config.get(prof_section, 'recipient_email', fallback='').strip()
            if profile_recipient:
                email_config['recipient_emails'] = [e.strip() for e in profile_recipient.split(',') if e.strip()]
                print(f"Using profile-specific email recipients: {', '.join(email_config['recipient_emails'])}")

        loop_desc = "infinite" if loop_tries == 0 else str(loop_tries)
        print(f"Configuration loaded. Funds: {funds_to_try}, Loops: {loop_desc}, Amount: {amount_to_buy}, Bank: {bank_to_use}, Session refresh: {session_refresh_interval} attempts")
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

        # 2. Initialize WebDriver
        print("Initializing WebDriver...")
        driver = get_webdriver("chrome") # Or get browser from config

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
                    time.sleep(min_wait + 1)
                else:
                    # Just skip this fund silently and try next
                    pass
                continue

            # Send periodic summary email
            maybe_send_summary_email(email_config)

            stats.record_attempt(current_fund)
            print(f"\n--- Attempting purchase for fund: {current_fund} (Attempt {fund_attempt_count}) ---")
            
            # Proactive session refresh after every N attempts to prevent stale sessions
            if fund_attempt_count > 1 and (fund_attempt_count - 1) % session_refresh_interval == 0:
                stats.record_refresh()
                chrome_restart_count += 1

                # Every Nth session refresh, also restart Chrome to prevent memory leaks
                if chrome_restart_count % CHROME_RESTART_INTERVAL == 0:
                    print(f"🔄 Full Chrome restart (refresh #{chrome_restart_count}) to free memory...")
                    try:
                        logout(driver)
                    except Exception:
                        pass
                    try:
                        driver.quit()
                        print("Old Chrome closed.")
                    except Exception:
                        pass
                    driver = get_webdriver("chrome")
                    if login(driver, username, password, security_phrase):
                        print("✅ Chrome restarted and logged in successfully.")
                    else:
                        print("❌ Re-login after Chrome restart failed. Exiting.")
                        return
                else:
                    print(f"🔄 Proactive session refresh after {fund_attempt_count - 1} attempts...")
                    if logout(driver):
                        print("Proactive logout successful. Re-logging in...")
                        if login(driver, username, password, security_phrase):
                            print("✅ Proactive session refresh successful. Continuing...")
                        else:
                            print("❌ Proactive re-login failed. Exiting.")
                            return
                    else:
                        print("⚠️ Proactive logout failed. Continuing without refresh...")

            # 4a. Navigate to Purchase Section for the current fund
            nav_result = navigate_to_purchase(driver, current_fund)

            if nav_result == "BLOCKED":
                print(f"Fund {current_fund} blocked at navigation stage. Applying cooldown.")
                stats.record_block(current_fund)
                fund_cooldowns.mark_blocked(current_fund)
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
            purchase_status = purchase_unit(driver, current_fund, amount_to_buy, bank_to_use, email_config)

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
                    driver.quit()
                except Exception:
                    pass
                driver = get_webdriver("chrome")
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
                stats.record_block(current_fund)
                fund_cooldowns.mark_blocked(current_fund)
                print(f"Purchase attempt for {current_fund} was blocked (1001). Cooldown {FundCooldownTracker.COOLDOWN_SECONDS}s. Trying next fund.")
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
        print("\n--- Ctrl+C detected. Attempting graceful shutdown... ---")
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
            # Clean up temp Chrome data dir
            try:
                chrome_data_dir = None
                for arg in driver.capabilities.get('chrome', {}).get('chromedriverArgs', []):
                    if 'user-data-dir' in str(arg):
                        chrome_data_dir = str(arg).split('=')[-1]
            except Exception:
                pass

            print("Closing WebDriver...")
            driver.quit()
            print("WebDriver closed.")

            # Clean up temp data dir
            if chrome_data_dir and chrome_data_dir.startswith(tempfile.gettempdir()) and 'chrome_asnb_' in chrome_data_dir:
                try:
                    shutil.rmtree(chrome_data_dir, ignore_errors=True)
                    print(f"Cleaned up temp Chrome dir: {chrome_data_dir}")
                except Exception:
                    pass
        print("--- Automation finished ---")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ASNB Buyer Automation")
    parser.add_argument("--profile", type=str, default=None,
                        help="Profile name to use from [Profile.<name>] section in config.ini")
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
            main(profile=args.profile)
        except KeyboardInterrupt:
            print("\n--- Script stopped (Ctrl+C or SIGTERM). ---")
            break
        except Exception:
            pass

        if _sigterm_received:
            break

        print("\n--- Restarting automation process in 3 seconds... ---")
        time.sleep(3)
