import subprocess
import unittest
from unittest.mock import Mock, patch

from asnb.health import BrowserHealth, browser_rss_mb


class MemoryTests(unittest.TestCase):
    @patch('asnb.health.subprocess.run')
    def test_only_current_browser_tree(self, run):
        run.return_value.stdout = '10 1 1024\n11 10 2048\n12 11 1024\n20 1 999999\n21 20 999999\n'
        driver = Mock(capabilities={'goog:processID': 10})
        self.assertEqual(browser_rss_mb(driver), 4)
        self.assertEqual(run.call_args.kwargs['timeout'], 2)

    @patch('asnb.health.subprocess.run')
    def test_driver_pid_fallback(self, run):
        run.return_value.stdout = '1 0 1024\n2 1 2048\n'
        driver = Mock(capabilities={})
        driver.service.process.pid = 1
        self.assertEqual(browser_rss_mb(driver), 3)

    @patch('asnb.health.subprocess.run', side_effect=subprocess.TimeoutExpired('ps', 2))
    def test_telemetry_failure_is_nonfatal(self, run):
        self.assertIsNone(browser_rss_mb(Mock(capabilities={'goog:processID': 10})))

    @patch('asnb.health.subprocess.run')
    def test_missing_process_is_unknown_not_zero(self, run):
        run.return_value.stdout = '20 1 1024\n'
        self.assertIsNone(browser_rss_mb(Mock(capabilities={'goog:processID': 10})))


class SchedulingTests(unittest.TestCase):
    @patch('asnb.health.browser_rss_mb', return_value=2000)
    def test_requires_sustained_memory_and_prefers_cooldown(self, rss):
        policy = BrowserHealth(limit_mb=1536)
        policy.sample(None, now=0)
        self.assertFalse(policy.restart_due(120, now=0))
        policy.sample(None, now=30)
        self.assertEqual(rss.call_count, 1)
        policy.sample(None, now=60)
        self.assertFalse(policy.restart_due(0, now=60))
        self.assertFalse(policy.restart_due(44, now=60))
        self.assertTrue(policy.restart_due(45, now=60))
        self.assertFalse(policy.restart_due(0, now=359))
        self.assertTrue(policy.restart_due(0, now=360))

    @patch('asnb.health.browser_rss_mb', side_effect=[2000, 2000, 1000])
    def test_memory_recovery_cancels_pending_restart(self, rss):
        policy = BrowserHealth(limit_mb=1536)
        for now in (0, 60):
            policy.sample(None, now=now)
        self.assertTrue(policy.restart_due(60, now=60))
        policy.sample(None, now=120)
        self.assertFalse(policy.restart_due(120, now=120))

    @patch('asnb.health.browser_rss_mb', side_effect=[2000, None, 2000])
    def test_unknown_readings_do_not_create_a_restart(self, rss):
        policy = BrowserHealth(limit_mb=1536)
        for now in (0, 60, 120):
            policy.sample(None, now=now)
        self.assertFalse(policy.restart_due(120, now=120))
        policy.reset()
        self.assertIsNone(policy.pending_since)
        self.assertIsNone(policy.last_sample)
