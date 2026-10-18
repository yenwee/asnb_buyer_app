import unittest
from unittest.mock import patch

from asnb.email import send_purchase_notification, test_email_config as send_test


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(
            smtp_server='smtp.example.com', smtp_port=587,
            sender_email='sender@example.com', sender_password='dummy',
            recipient_emails=['recipient@example.com'], send_on_success=False,
        )

    @patch('asnb.email.smtplib.SMTP')
    def test_test_email_bypasses_alert_toggle_and_labels_message(self, smtp):
        server = smtp.return_value.__enter__.return_value
        server.sendmail.return_value = {}
        self.assertTrue(send_test(self.config))
        smtp.assert_called_once_with('smtp.example.com', 587, timeout=30)
        server.starttls.assert_called_once()
        server.set_debuglevel.assert_not_called()
        message = server.sendmail.call_args.args[2]
        self.assertIn('TEST -', message)
        self.assertIn('No purchase was attempted.', message)
        self.assertNotIn('Purchase Successful!', message)

    @patch('asnb.email.smtplib.SMTP_SSL')
    def test_ssl_and_recipient_refusal(self, smtp):
        self.config['smtp_port'] = 465
        server = smtp.return_value.__enter__.return_value
        server.sendmail.return_value = {'recipient@example.com': (550, b'rejected')}
        self.assertFalse(send_test(self.config))
        server.starttls.assert_not_called()

    @patch('asnb.email.smtplib.SMTP', side_effect=OSError('connection closed'))
    def test_connection_failure_returns_false(self, smtp):
        self.config['send_on_success'] = True
        self.assertFalse(send_purchase_notification(self.config, success=True))
