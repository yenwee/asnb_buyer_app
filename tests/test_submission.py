import unittest
import threading
from unittest.mock import Mock, patch

from selenium.common.exceptions import WebDriverException, TimeoutException

from asnb import main
from asnb.purchase_state import submit_and_observe


class SubmissionTests(unittest.TestCase):
    def test_notification_uses_valid_sound_name_and_no_shell_quoting(self):
        with patch.object(main.platform, 'system', return_value='Darwin'), \
                patch.object(main.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            main.send_desktop_notification('A "title"', "It's ready", sound=True)
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ['osascript','-e'])
        self.assertIn('sound name "Glass"', args[2])
        self.assertNotIn('with sound', args[2])
        self.assertIn("It's ready", args[2])
        self.assertFalse(run.call_args.kwargs.get('shell',False))

    def test_slow_success_email_is_bounded_and_does_not_block(self):
        entered, release = threading.Event(), threading.Event()
        previous = main._success_email_thread
        def send(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return True
        try:
            main._success_email_thread = None
            with patch.object(main, 'send_purchase_notification', side_effect=send) as smtp:
                main.send_success_email_async({}, 'fund', '100', [])
                self.assertTrue(entered.wait(timeout=1))
                self.assertTrue(main._success_email_thread.is_alive())
                main.send_success_email_async({}, 'fund', '100', [])
                self.assertEqual(smtp.call_count, 1)
                release.set()
                main._success_email_thread.join(timeout=2)
                self.assertFalse(main._success_email_thread.is_alive())
        finally:
            release.set()
            if main._success_email_thread:
                main._success_email_thread.join(timeout=2)
            main._success_email_thread = previous

    @patch('asnb.purchase_state.wait_purchase_state', return_value='SUCCESS')
    def test_click_error_can_still_mean_success_and_never_replays(self, wait):
        button = Mock()
        button.click.side_effect = WebDriverException('lost acknowledgement')
        self.assertEqual(submit_and_observe(Mock(), button), 'SUCCESS')
        button.click.assert_called_once()

    @patch('asnb.purchase_state.wait_purchase_state', side_effect=TimeoutException)
    def test_unknown_response_is_uncertain_not_a_retry(self, wait):
        button = Mock()
        self.assertEqual(submit_and_observe(Mock(), button), 'UNCERTAIN')
        button.click.assert_called_once()

    def test_navigation_distinguishes_unavailable_from_blocked(self):
        for text, expected in [('insufficient units available (068)', 'INSUFFICIENT'),
                               ('Blocked due to retry (1001)', 'BLOCKED')]:
            driver = Mock()
            driver.find_element.return_value.text = text
            with patch.object(main, 'wait_for_clickable_any', return_value=('OK', Mock())):
                self.assertEqual(main.detect_and_dismiss_block_popup(driver), expected)

    def test_desktop_alert_precedes_email_and_payment_pause(self):
        events = []
        with patch.object(main, 'wait_for_element', return_value=Mock()), \
                patch.object(main, 'wait_for_clickable_element', return_value=Mock()), \
                patch.object(main, 'Select', return_value=Mock(options=[1, 2])), \
                patch.object(main, 'human_type'), patch.object(main, 'between_actions'), \
                patch.object(main, 'submit_and_observe', return_value='SUCCESS'), \
                patch.object(main, 'bring_browser_to_front'), \
                patch.object(main, 'send_desktop_notification', side_effect=lambda *a, **kw: events.append('desktop')), \
                patch.object(main, 'send_success_email_async', side_effect=lambda *a: events.append('email')), \
                patch.object(main.platform, 'system', return_value='test'), \
                patch('builtins.input', side_effect=EOFError):
            with self.assertRaises(KeyboardInterrupt):
                main.purchase_unit(Mock(), 'fund', '100', 'Public Bank', email_config={'test': True})
        self.assertEqual(events, ['desktop', 'email'])

    def test_review_eof_stops_instead_of_restarting(self):
        with patch('builtins.input', side_effect=EOFError), \
                patch.object(main, 'send_desktop_notification'):
            with self.assertRaises(KeyboardInterrupt):
                main.wait_for_submission_review(Mock())

    def test_gui_can_resume_review_and_later_payments(self):
        from asnb.gui import AccountRunner
        button = Mock()
        runner = AccountRunner('test', {}, Mock(), Mock(), Mock(), resume_btn=button)
        runner.log = Mock()
        runner._show_review()
        button.configure.assert_called_with(state='normal')
        button.reset_mock()
        runner._show_resume()
        button.configure.assert_called_with(state='normal')

    def test_web_exposes_review_and_clears_status_when_process_exits(self):
        from asnb.web import WebRunner
        runner = WebRunner('test')
        def output():
            yield '[REVIEW_REQUIRED]\n'
            self.assertEqual(runner.snapshot()['status'], 'review')
        runner.process = Mock(stdout=output())
        runner._read_output()
        self.assertEqual(runner.snapshot()['status'], 'stopped')
