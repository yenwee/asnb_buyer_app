import unittest
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options

from asnb.purchase_state import find_fund_action, wait_purchase_state, block_retry_seconds


class PurchaseStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        cls.driver = webdriver.Chrome(options=options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def load(self, html):
        self.driver.get('data:text/html,' + quote(html))

    def test_exact_fund_and_both_languages(self):
        self.load('''<div><div><span>Amanah Saham Malaysia 2</span></div>
            <span id="two">Add Invest</span></div>
            <div><div><span>Amanah Saham Malaysia</span></div>
            <span id="one">Pelaburan Tambahan</span></div>''')
        for name, expected in [('Amanah Saham Malaysia', 'one'), ('Amanah Saham Malaysia 2', 'two')]:
            self.assertEqual(find_fund_action(self.driver, [name]).get_attribute('id'), expected)
        self.assertIsNone(find_fund_action(self.driver, ['Amanah Saham Malaysia 3']))

    def test_does_not_borrow_another_cards_action(self):
        self.load('''<main><div><span>Amanah Saham Malaysia</span></div>
            <div><span>Amanah Saham Malaysia 2</span><button>Add Invest</button></div></main>''')
        self.assertIsNone(find_fund_action(self.driver, ['Amanah Saham Malaysia']))

    def test_live_class_a_labels_with_hidden_action_menus(self):
        self.load('''<main>
            <div><div><span>Amanah Saham Malaysia (Class  A)</span></div>
              <div style="display:none"><span id="one">Add Invest</span></div></div>
            <div><div><span>Amanah Saham Malaysia 2 - Wawasan (Class  A)</span></div>
              <div style="display:none"><span id="two">Add Invest</span></div></div>
            <div><div><span>Amanah Saham Malaysia 3 (Class  A)</span></div>
              <div style="display:none"><span id="three">Add Invest</span></div></div>
            </main>''')
        for name, expected in [('Amanah Saham Malaysia', 'one'),
                               ('Amanah Saham Malaysia 2 - Wawasan', 'two'),
                               ('Amanah Saham Malaysia 3', 'three')]:
            self.assertEqual(find_fund_action(self.driver, [name]).get_attribute('id'), expected)

    def test_class_b_is_not_implicitly_selected(self):
        self.load('''<div><span>Amanah Saham Malaysia (Class B)</span>
            <span>Add Invest</span></div>''')
        self.assertIsNone(find_fund_action(self.driver, ['Amanah Saham Malaysia']))

    def test_wrapping_fund_card_anchor_is_not_returned_as_action(self):
        self.load('''<a href="#fund"><div><span>Amanah Saham Malaysia (Class A)</span></div>
            <div style="display:none"><span id="action">Add Invest</span></div></a>''')
        element = find_fund_action(self.driver, ['Amanah Saham Malaysia'])
        self.assertEqual(element.get_attribute('id'), 'action')
        self.assertEqual(element.text, '')  # Menu is hidden; native JS dispatch uses the span.

    def test_outcomes_including_delayed_error(self):
        for html, navigation, expected in [
            ('<p>insufficient units available (068)</p>', False, 'INSUFFICIENT'),
            ('<p>Blocked due to retry (1001)</p>', False, 'BLOCKED'),
            ('<input name="amount">', True, 'AMOUNT'),
            ('<div>Dana Harga Tetap Kelas A</div>', True, 'CLASS'),
            ('<input type="checkbox" value="agree"><button>Teruskan Pembayaran</button>', False, 'SUCCESS'),
            ('<p style="display:none">(1001)</p><script>setTimeout(()=>document.body.innerHTML="<p>(068)</p>",300)</script>', False, 'INSUFFICIENT'),
        ]:
            with self.subTest(expected=expected):
                self.load(html)
                self.assertEqual(wait_purchase_state(self.driver, navigation, timeout=2), expected)

    def test_unknown_state_times_out_without_retry(self):
        self.load('<button>Next</button>')
        with self.assertRaises(TimeoutException):
            wait_purchase_state(self.driver, timeout=0.5)

    def test_live_div_modal_detects_block_and_retry_instruction(self):
        self.load('''<div class="fixed inset-0"><div class="text-neutral60">
            Blocked due to too many retry. Please try again in 5 minutes.
            </div><button>Close</button></div>''')
        self.assertEqual(wait_purchase_state(self.driver, navigation=True, timeout=1), 'BLOCKED')
        self.assertEqual(self.driver._asnb_block_retry_seconds, 300)

    def test_retry_instruction_units_and_fallback(self):
        self.assertEqual(block_retry_seconds('Please try again in 5 minutes.'), 300)
        self.assertEqual(block_retry_seconds('Try again in 45 seconds.'), 45)
        self.assertEqual(block_retry_seconds('Cuba semula dalam 2 minit'), 120)
        self.assertEqual(block_retry_seconds('Unknown error'), 300)
        self.assertEqual(block_retry_seconds('Try again in 0 seconds'), 300)
