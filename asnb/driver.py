from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import WebDriverException
from typing import Type, Union
import os
import platform
import time
import subprocess

# Define WebDriver type for hinting (using Union for Python < 3.10 compatibility)
WebDriver = Union[Type[webdriver.Chrome], Type[webdriver.Firefox], Type[webdriver.Edge]] # Add others if needed

class DriverSetupError(Exception):
    """Custom exception for WebDriver setup errors."""
    pass

def get_webdriver(browser: str = "chrome") -> WebDriver:
    """
    Initializes and returns a Selenium WebDriver instance for the specified browser.

    Uses webdriver-manager to automatically download and manage the driver.

    Args:
        browser: The name of the browser to use (e.g., "chrome", "firefox").
                 Defaults to "chrome".

    Returns:
        An initialized Selenium WebDriver instance.

    Raises:
        DriverSetupError: If the specified browser is not supported or
                          if WebDriver setup fails.
    """
    try:
        chrome_options = ChromeOptions()
        
        # ARM64 Mac-specific Chrome options to prevent status code -9 issues
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            print("Detected ARM64 Mac - applying aggressive compatibility options...")
            # Core stability options
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--disable-software-rasterizer")
            chrome_options.add_argument("--disable-background-timer-throttling")
            chrome_options.add_argument("--disable-backgrounding-occluded-windows")
            chrome_options.add_argument("--disable-renderer-backgrounding")
            
            # Additional ARM64 Mac stability options
            chrome_options.add_argument("--disable-features=VizDisplayCompositor")
            chrome_options.add_argument("--disable-gpu-sandbox")
            chrome_options.add_argument("--disable-logging")
            chrome_options.add_argument("--disable-login-animations")
            chrome_options.add_argument("--no-first-run")
            chrome_options.add_argument("--no-default-browser-check")
            chrome_options.add_argument("--disable-default-apps")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-component-extensions-with-background-pages")
            chrome_options.add_argument("--disable-background-networking")
            chrome_options.add_argument("--disable-sync")
            chrome_options.add_argument("--disable-background-downloads")
            
            # Use a fresh user data directory each time to avoid stale lock files
            # When Chrome is killed without proper shutdown, it leaves SingletonLock
            # in the data dir, preventing new instances from starting
            import tempfile
            import glob

            # Clean up any stale chrome_asnb_ dirs from previous runs
            stale_dirs = glob.glob(os.path.join(tempfile.gettempdir(), "chrome_asnb_*"))
            for stale_dir in stale_dirs:
                try:
                    import shutil
                    shutil.rmtree(stale_dir, ignore_errors=True)
                except Exception:
                    pass

            chrome_data_dir = tempfile.mkdtemp(prefix="chrome_asnb_")
            chrome_options.add_argument(f"--user-data-dir={chrome_data_dir}")
            
        # General Chrome stability + stealth options
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # Realistic user-agent (Chrome on macOS)
        chrome_options.add_argument(
            "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        chrome_options.add_argument("--disable-infobars")
        
        if browser.lower() == "chrome":
            try:
                print("Ensuring ChromeDriver is available...")
                
                # Download ChromeDriver if needed (this ensures it's available for auto-detection)
                ChromeDriverManager().install()
                
                print("Initializing Chrome WebDriver (auto-detect mode for ARM64 compatibility)...")
                
                # Use auto-detection method that works reliably on ARM64 Mac
                # This avoids the status code -9 issue by letting Chrome find its own driver
                driver = webdriver.Chrome(options=chrome_options)
                
                # Mask navigator.webdriver before any navigation
                driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": """
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                        window.chrome = {runtime: {}};
                    """
                })

                # Test the driver with a simple operation
                print("Testing WebDriver connection...")
                driver.set_page_load_timeout(30)
                
                print("ChromeDriver initialized successfully!")
            except WebDriverException as e:
                if "cannot find Chrome binary" in str(e):
                    print("Chrome binary not found, attempting to use Brave instead.")
                    # Assuming Brave is in the default location, adjust if needed
                    brave_path = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
                    if os.path.exists(brave_path):
                        chrome_options.binary_location = brave_path
                        # Apply the same robust path and permission logic for Brave fallback
                        installed_path_brave = ChromeDriverManager().install()
                        driver_dir_brave = os.path.dirname(installed_path_brave)
                        executable_path_brave = os.path.join(driver_dir_brave, "chromedriver")

                        if not os.path.isfile(executable_path_brave):
                            raise DriverSetupError(f"ChromeDriver executable (for Brave) not found at expected path: {executable_path_brave}")

                        if not os.access(executable_path_brave, os.X_OK):
                            print(f"Attempting to set execute permission for Brave fallback driver: {executable_path_brave}")
                            try:
                                os.chmod(executable_path_brave, 0o755)
                                if not os.access(executable_path_brave, os.X_OK):
                                    raise DriverSetupError(f"Failed to set execute permission for Brave fallback driver: {executable_path_brave}")
                            except OSError as perm_error_brave:
                                raise DriverSetupError(f"Error setting execute permission for Brave fallback driver {executable_path_brave}: {perm_error_brave}") from perm_error_brave

                        service = ChromeService(executable_path=executable_path_brave)
                        driver = webdriver.Chrome(service=service, options=chrome_options)
                    else:
                        raise DriverSetupError("Brave Browser not found in default location. Please install Chrome or Brave.") from e
                else:
                    raise # Re-raise the exception if it's not "cannot find Chrome binary"
        # Add elif blocks for other browsers like firefox, edge if needed
        # elif browser.lower() == "firefox":
        #     from selenium.webdriver.firefox.service import Service as FirefoxService
        #     from webdriver_manager.firefox import GeckoDriverManager
        #     service = FirefoxService(GeckoDriverManager().install())
        #     driver = webdriver.Firefox(service=service)
        else:
            raise DriverSetupError(f"Unsupported browser: {browser}")

        print(f"{browser.capitalize()} WebDriver initialized successfully.")
        return driver
    except WebDriverException as e:
        raise DriverSetupError(f"Failed to initialize {browser} WebDriver: {e}") from e
    except Exception as e:
        # Catch other potential errors during driver download/setup
        raise DriverSetupError(f"An unexpected error occurred during WebDriver setup: {e}") from e

# Example usage (optional, for testing the module directly)
if __name__ == "__main__":
    driver_instance = None
    try:
        print("Attempting to set up Chrome WebDriver...")
        driver_instance = get_webdriver("chrome")
        driver_instance.get("https://www.google.com") # Test navigation
        print("Successfully navigated to Google.")
        # Add similar tests for other browsers if implemented
    except DriverSetupError as e:
        print(f"Driver Setup Error: {e}")
    finally:
        if driver_instance:
            print("Closing WebDriver.")
            driver_instance.quit()

