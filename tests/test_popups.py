"""Browser regression checks: python -m unittest discover -s tests."""

import unittest
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options

from asnb.popups import dismiss_login_popups


class LoginPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument('--headless=new')
        cls.driver = webdriver.Chrome(options=options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def load(self, script):
        self.driver.get('data:text/html,' + quote('''
            <style>.fixed{position:fixed;inset:0;background:#888}
            .absolute{position:absolute;top:0;right:0;width:30px;height:30px}</style>
            <input name="username">
            <script>function popup(){
                const e=document.createElement('div');
                e.className='bg-black fixed transition-opacity inset-0 opacity-25';
                e.innerHTML='<svg class="absolute cursor-pointer" onclick="this.parentElement.remove()"><rect width="30" height="30"/></svg>';
                document.body.append(e);
            }
            ''' + script + '</script>'))

    def test_absent_reordered_delayed_and_stacked(self):
        for script in ('', 'popup()', 'setTimeout(()=>{popup();popup()},600)'):
            with self.subTest(script=script):
                self.load(script)
                dismiss_login_popups(self.driver, appearance_wait=1, timeout=4)
                self.assertEqual(self.driver.execute_script(
                    "return document.querySelectorAll('div.fixed').length"), 0)

    def test_unclosable_overlay_fails(self):
        self.load("popup();document.querySelector('svg').remove()")
        with self.assertRaises(TimeoutException):
            dismiss_login_popups(self.driver, appearance_wait=0, timeout=1)

    def test_click_without_dismissal_fails(self):
        self.load("popup();document.querySelector('svg').onclick=()=>{}")
        with self.assertRaises(TimeoutException):
            dismiss_login_popups(self.driver, appearance_wait=0, timeout=1)
