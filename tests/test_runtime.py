import tempfile
import configparser
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from asnb import main
from asnb.driver import close_webdriver, get_webdriver, DriverSetupError


class BrowserCleanupTests(unittest.TestCase):
    def test_cleanup_only_owned_profile_after_quit(self):
        with tempfile.TemporaryDirectory() as root:
            owned = Path(root, 'chrome_asnb_owned')
            other = Path(root, 'chrome_asnb_other')
            owned.mkdir()
            other.mkdir()
            driver = Mock(_asnb_data_dir=str(owned))
            driver.quit.side_effect = lambda: self.assertTrue(owned.exists())
            close_webdriver(driver)
            self.assertFalse(owned.exists())
            self.assertTrue(other.exists())

    def test_failed_quit_preserves_profile(self):
        driver = Mock(_asnb_data_dir='/unused')
        driver.quit.side_effect = RuntimeError('still running')
        with patch('asnb.driver.shutil.rmtree') as remove:
            with self.assertRaises(RuntimeError):
                close_webdriver(driver)
            remove.assert_not_called()

    def test_failed_start_cleans_its_new_profile(self):
        with tempfile.TemporaryDirectory() as root:
            owned = Path(root, 'owned')
            owned.mkdir()
            with patch('asnb.driver.platform.system', return_value='Darwin'), \
                    patch('asnb.driver.platform.machine', return_value='arm64'), \
                    patch('asnb.driver.tempfile.mkdtemp', return_value=str(owned)), \
                    patch('asnb.driver.ChromeDriverManager') as manager, \
                    patch('asnb.driver.webdriver.Chrome', side_effect=RuntimeError('startup failed')):
                manager.return_value.install.return_value = 'unused'
                with self.assertRaises(DriverSetupError):
                    get_webdriver()
            self.assertFalse(owned.exists())


class SummaryTests(unittest.TestCase):
    def test_slow_email_does_not_block_or_spawn_more_workers(self):
        entered, release = threading.Event(), threading.Event()

        def slow_send(**kwargs):
            entered.set()
            release.wait(timeout=5)
            return True

        previous_thread = main._summary_thread
        previous_time = main._last_summary_time
        try:
            main._summary_thread = None
            main._last_summary_time = 0
            with patch.object(main, 'send_purchase_notification', side_effect=slow_send) as send:
                main.maybe_send_summary_email({'send_on_failure': False})
                self.assertTrue(entered.wait(timeout=1))
                self.assertTrue(main._summary_thread.is_alive())
                main._last_summary_time = 0
                main.maybe_send_summary_email({'send_on_failure': False})
                self.assertEqual(send.call_count, 1)
                release.set()
                main._summary_thread.join(timeout=2)
                self.assertFalse(main._summary_thread.is_alive())
        finally:
            release.set()
            if main._summary_thread:
                main._summary_thread.join(timeout=2)
            main._summary_thread = previous_thread
            main._last_summary_time = previous_time

    def test_portfolio_waits_for_readiness_without_fixed_sleep(self):
        driver = Mock()
        with patch.object(main, 'wait_for_clickable_any', return_value=('Span', Mock())) as find, \
                patch.object(main, 'WebDriverWait') as wait, \
                patch.object(main.time, 'sleep') as sleep:
            self.assertTrue(main.navigate_to_portfolio(driver))
            self.assertEqual(find.call_count, 1)
            self.assertEqual(wait.return_value.until.call_count, 2)
            sleep.assert_not_called()


class MaintenanceLoopTests(unittest.TestCase):
    def test_api_recovery_refreshes_stale_ui_once_without_relogin(self):
        from asnb.api import PortalAPI
        driver = Mock(current_url=main.MYASNB_PORTFOLIO_URL, capabilities={})
        driver._asnb_portfolio_refresh_needed = True
        client = Mock(spec=PortalAPI)
        client.metrics = {}
        client.preflight.return_value = {'Amanah Saham Malaysia': {'state': 'ELIGIBLE', 'fund_id': 'ASM'}}
        policy = Mock()
        policy.sample.return_value = None
        policy.restart_due.return_value = False
        with patch.object(main, 'load_config', return_value=self.config()), \
                patch.object(main, 'setup_logging'), \
                patch.object(main, 'get_webdriver', return_value=driver), \
                patch.object(main, 'PortalAPI', return_value=client), \
                patch.object(main, 'close_webdriver'), \
                patch.object(main, 'login', return_value=True) as login, \
                patch.object(main, 'navigate_to_purchase', return_value=True), \
                patch.object(main, 'purchase_unit', return_value=False) as purchase, \
                patch.object(main, 'navigate_to_portfolio', return_value=True), \
                patch.object(main, 'verify_funds_loaded', return_value=True), \
                patch.object(main, 'find_fund_action', return_value=Mock()), \
                patch.object(main, 'BrowserHealth', return_value=policy), \
                patch.object(main, 'fund_cooldowns') as cooldowns:
            cooldowns.is_on_cooldown.return_value = False
            main.main('test', purchase_transport='ui')
            driver.refresh.assert_called_once()
            self.assertEqual(login.call_count, 1)
            self.assertEqual(purchase.call_count, 7)
            self.assertEqual(client.preflight.call_count, 7)

    def test_default_api_mode_delegates_without_creating_browser(self):
        with patch.object(main, 'load_config', return_value=self.config()), \
                patch.object(main, 'setup_logging'), \
                patch.object(main, 'get_webdriver') as create, \
                patch.object(main, 'login') as ui_login, \
                patch.object(main, 'purchase_unit') as ui_purchase, \
                patch('asnb.api_runner.run_http_runner', side_effect=KeyboardInterrupt) as http_run:
            with self.assertRaises(KeyboardInterrupt):
                main.main('test')
        create.assert_not_called()
        ui_login.assert_not_called()
        ui_purchase.assert_not_called()
        http_run.assert_called_once()
        self.assertEqual(http_run.call_args.kwargs['subscription_transport'], 'http')

    def test_cooldown_uses_elapsed_time(self):
        tracker = main.FundCooldownTracker()
        with patch.object(main.time, 'monotonic', return_value=100):
            tracker.mark_blocked('fund')
        with patch.object(main.time, 'monotonic', return_value=160):
            self.assertTrue(tracker.is_on_cooldown('fund'))
            self.assertEqual(tracker.remaining_cooldown('fund'), 240)
        with patch.object(main.time, 'monotonic', return_value=400):
            self.assertFalse(tracker.is_on_cooldown('fund'))

    def config(self):
        config = configparser.ConfigParser()
        config.read_dict({
            'Settings': {'funds_to_try': 'Amanah Saham Malaysia',
                         'purchase_amount': '100', 'loop_tries': '7',
                         'session_refresh_interval': '6'},
            'Profile.test': {'username': 'dummy', 'password': 'dummy', 'security_phrase': ''},
        })
        return config

    def test_healthy_session_keeps_browser_and_login(self):
        driver = Mock(current_url=main.MYASNB_PORTFOLIO_URL)
        policy = Mock()
        policy.sample.return_value = None
        policy.restart_due.return_value = False
        with patch.object(main, 'load_config', return_value=self.config()), \
                patch.object(main, 'setup_logging'), \
                patch.object(main, 'get_webdriver', return_value=driver) as create, \
                patch.object(main, 'close_webdriver') as close, \
                patch.object(main, 'login', return_value=True) as login, \
                patch.object(main, 'navigate_to_purchase', return_value=True), \
                patch.object(main, 'purchase_unit', return_value=False) as purchase, \
                patch.object(main, 'navigate_to_portfolio', return_value=True), \
                patch.object(main, 'verify_funds_loaded', return_value=True), \
                patch.object(main, 'find_fund_action', return_value=Mock()), \
                patch.object(main, 'BrowserHealth', return_value=policy), \
                patch.object(main, 'fund_cooldowns') as cooldowns:
            cooldowns.is_on_cooldown.return_value = False
            main.main('test', purchase_transport='ui')
            self.assertEqual(create.call_count, 1)
            self.assertEqual(login.call_count, 1)
            self.assertEqual(purchase.call_count, 7)
            close.assert_called_once_with(driver)

    def test_maintenance_time_counts_toward_cooldown(self):
        old_driver, new_driver = Mock(), Mock()
        policy = Mock()
        policy.sample.return_value = None
        policy.restart_due.return_value = True
        with patch.object(main, 'load_config', return_value=self.config()), \
                patch.object(main, 'setup_logging'), \
                patch.object(main, 'get_webdriver', side_effect=[old_driver, new_driver]), \
                patch.object(main, 'close_webdriver'), \
                patch.object(main, 'login', return_value=True), \
                patch.object(main, 'logout', return_value=True), \
                patch.object(main, 'send_desktop_notification'), \
                patch.object(main, 'BrowserHealth', return_value=policy), \
                patch.object(main, 'fund_cooldowns') as cooldowns, \
                patch.object(main.time, 'sleep', side_effect=KeyboardInterrupt) as sleep:
            cooldowns.is_on_cooldown.return_value = True
            cooldowns.remaining_cooldown.side_effect = [100, 20, 20]
            with self.assertRaises(KeyboardInterrupt):
                main.main('test', purchase_transport='ui')
            policy.restart_due.assert_called_once_with(100)
            sleep.assert_called_once_with(20.25)
            policy.reset.assert_called_once()
