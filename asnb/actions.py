"""Human-like interaction helpers for Selenium automation.

Simulates realistic human behavior: mouse movements, typing speed,
variable delays, and natural click patterns.
"""

import time
import random
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement


def human_delay(min_sec=0.5, max_sec=2.0):
    """Pause for a random human-like duration."""
    time.sleep(random.uniform(min_sec, max_sec))


def human_type(element: WebElement, text: str, min_delay=0.04, max_delay=0.18):
    """Type text character by character with realistic keystroke timing.

    Humans don't type at constant speed - there are micro-pauses between
    characters, occasional longer pauses (thinking), and burst typing.
    """
    for i, char in enumerate(text):
        element.send_keys(char)

        # Occasional longer pause (simulates thinking/hesitation)
        if random.random() < 0.08:
            time.sleep(random.uniform(0.3, 0.6))
        else:
            time.sleep(random.uniform(min_delay, max_delay))


def human_click(driver: WebDriver, element: WebElement, pause_before=True):
    """Click an element with realistic mouse movement and timing.

    Moves mouse to element first, pauses briefly (like a human aiming),
    then clicks. Optionally adds a small delay before the move.
    """
    if pause_before:
        time.sleep(random.uniform(0.2, 0.5))

    try:
        actions = ActionChains(driver)
        actions.move_to_element(element)
        actions.pause(random.uniform(0.1, 0.4))
        actions.click()
        actions.perform()
    except Exception:
        # Fallback to regular click if ActionChains fails (e.g., obscured element)
        element.click()

    time.sleep(random.uniform(0.3, 0.8))


def human_js_click(driver: WebDriver, element: WebElement):
    """JavaScript click with human-like delay before and after.

    Used when regular clicks fail due to overlays or SVG elements.
    Adds natural timing around the JS execution.
    """
    time.sleep(random.uniform(0.3, 0.7))
    driver.execute_script("arguments[0].click();", element)
    time.sleep(random.uniform(0.4, 0.9))


def human_scroll_to(driver: WebDriver, element: WebElement):
    """Scroll element into view with a slight delay, like a human would."""
    driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
    time.sleep(random.uniform(0.5, 1.0))


def between_actions(min_sec=0.8, max_sec=2.5):
    """Wait between page interactions like a human reading/deciding."""
    time.sleep(random.uniform(min_sec, max_sec))


def after_page_load(min_sec=1.5, max_sec=3.5):
    """Wait after a page loads, simulating reading/orientation time."""
    time.sleep(random.uniform(min_sec, max_sec))
