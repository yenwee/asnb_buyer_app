import fnmatch
import unittest
from unittest.mock import Mock, patch

from asnb.assets import configure_assets, DECORATIVE_ASSETS
from asnb.driver import get_webdriver, close_webdriver


class AssetTests(unittest.TestCase):
    def test_only_decorative_assets_match(self):
        blocked = [
            'https://cdn.myasnb.com.my/banner/hero-banner8-min.png',
            'https://cdn.myasnb.com.my/banner/hero-banner-mobile1-min.png?v=1',
            'https://myasnb.oss-ap-southeast-3.aliyuncs.com/portal-rebrand/image/portal-pandubanner02_desktop.png',
        ]
        retained = [
            'https://cdn.myasnb.com.my/security-image/example.png',
            'https://cdn.myasnb.com.my/scam/scam_alert.png',
            'https://cdn.myasnb.com.my/portal-rebrand/image/myasnblogo-white.png',
            'https://studio.asnb.com.my/assets/search-close.svg',
            'https://www.myasnb.com.my/api/subscribe',
            'https://bank.example/payment/logo.png',
        ]
        for url in blocked:
            self.assertTrue(any(fnmatch.fnmatchcase(url, pattern) for pattern in DECORATIVE_ASSETS))
        for url in retained:
            self.assertFalse(any(fnmatch.fnmatchcase(url, pattern) for pattern in DECORATIVE_ASSETS))

    def test_switch_off_clears_blocklist_and_preserves_cache(self):
        driver = Mock()
        configure_assets(driver, lean_assets=False)
        driver.execute_cdp_cmd.assert_any_call('Network.setBlockedURLs', {'urls': []})
        driver.execute_cdp_cmd.assert_any_call('Network.setCacheDisabled', {'cacheDisabled': False})

    def test_browser_sets_eager_before_creation_and_rules_before_navigation(self):
        driver = Mock()
        with patch('asnb.driver.platform.system', return_value='Linux'), \
                patch('asnb.driver.ChromeDriverManager'), \
                patch('asnb.driver.webdriver.Chrome', return_value=driver) as chrome:
            self.assertIs(get_webdriver(), driver)
            self.assertEqual(chrome.call_args.kwargs['options'].page_load_strategy, 'eager')
            driver.execute_cdp_cmd.assert_any_call('Network.setBlockedURLs', {'urls': DECORATIVE_ASSETS})
            driver.get.assert_not_called()
