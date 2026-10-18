// Run with PLAYWRIGHT_MODULE set to an installed playwright package directory.
// Every browser request is intercepted. No authenticated context is attached.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const crypto = require('crypto');
const {spawnSync} = require('child_process');
const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, 'output/playwright/handoff');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/portal-assets.json'), 'utf8'));
for (const [name, expected] of Object.entries(manifest.files)) {
  const asset = path.join(artifacts, name);
  assert.ok(fs.existsSync(asset), `Missing ${name}; run .venv/bin/python tests/fetch_handoff_assets.py`);
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(asset)).digest('hex'), expected,
    `${name}: pinned asset changed; review frontend contract before updating the manifest`);
}
const metadata = spawnSync(path.join(root, '.venv/bin/python'), ['-c', `
import json
from unittest.mock import Mock, patch
from tests.test_handoff import fixture, metadata_driver
from asnb.handoff import confirmation_state, open_confirmation
from asnb.api_purchase import submit_api_and_restore
response, context = fixture()
state = confirmation_state(response, context)
driver = Mock()
driver.execute_script.return_value = True
open_confirmation(driver, state, context['account'])
bridge, check = [c.args[0] for c in driver.execute_script.call_args_list]
driver = metadata_driver(context)
driver.execute_async_script.return_value = {'response': response}
with patch('asnb.api_purchase.open_confirmation'), patch('builtins.print'):
    submit_api_and_restore(driver, context['fund_name'], '5000', context['bankname'])
print(json.dumps(dict(state=state, context=context, response=response,
    bridge=bridge, check=check,
    submit=driver.execute_async_script.call_args.args[0])))
`], {cwd: root, encoding: 'utf8'});
assert.equal(metadata.status, 0, metadata.stderr);
const data = JSON.parse(metadata.stdout);

async function main() {
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  const context = await browser.newContext({serviceWorkers: 'block', viewport: {width: 1200, height: 1000}});
  let unexpected = [];
  try {
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'preview.invalid' && ['/', '/preview.js'].includes(url.pathname)) {
        return route.fulfill({path: path.join(root, 'tests/fixtures/confirmation-preview', url.pathname === '/' ? 'index.html' : 'preview.js')});
      }
      if (url.hostname === 'www.myasnb.com.my' && ['/transactions', '/portfolio'].includes(url.pathname))
        return route.fulfill({contentType: 'text/html', body: '<div id="root"></div><script type="module" src="/harness.js"></script>'});
      if (url.hostname === 'www.myasnb.com.my' && url.pathname === '/harness.js')
        return route.fulfill({contentType: 'application/javascript', body: harness()});
      if (url.hostname === 'www.myasnb.com.my' && /^\/vendor-[\w-]+\.js$/.test(url.pathname))
        return route.fulfill({contentType: 'application/javascript', path: path.join(artifacts, url.pathname.slice(1))});
      unexpected.push({method: route.request().method(), path: url.pathname});
      return route.abort();
    });
    const page = await context.newPage();
    await page.goto('https://preview.invalid/');
    await page.waitForFunction(() => !!window.preview);
    assert.equal(await page.locator('#fund').innerText(), 'ASM 3 (Class A)');
    assert.equal(await page.locator('#amount').innerText(), 'RM5,000.00');
    assert.equal(await page.locator('#bank').innerText(), 'Hong Leong Bank');
    assert.equal(await page.locator('#agreement').isChecked(), false);
    assert.equal(await page.locator('#agreement').isDisabled(), true);
    assert.equal(await page.locator('#payment').isDisabled(), true);
    await page.screenshot({path: path.join(artifacts, 'offline-preview.png'), fullPage: true});
    const cases = [];
    for (const name of ['missing-state', 'missing-fpx', 'expired', 'malformed']) {
      await page.selectOption('#scenario', name);
      assert.equal(await page.locator('#error').isVisible(), true);
      assert.equal(await page.locator('#confirmation').isVisible(), false);
      assert.match(await page.locator('#error').innerText(), /no submission or retry/i);
      assert.equal(await page.locator('#payment').isDisabled(), true);
      cases.push({case: name, error: await page.locator('#error').innerText(), passed: true});
    }
    // Exercise connection and form guards, not only the disabled-button attribute.
    await page.evaluate(async () => {
      await fetch('https://example.invalid/blocked', {method: 'POST'}).catch(() => {});
      const form = document.createElement('form');
      form.action = 'https://example.invalid/blocked'; form.method = 'POST';
      document.body.append(form); form.submit(); form.remove();
      document.querySelector('#payment').dispatchEvent(new MouseEvent('click', {bubbles: true}));
    });
    await page.waitForTimeout(100);
    assert.equal(page.url(), 'https://preview.invalid/');

    // Actual downloaded portal component + actual React Router. Only surrounding
    // settings, translations and site chrome are stubbed; no FPX route is mounted.
    const actual = await context.newPage();
    const errors = [];
    actual.on('pageerror', error => errors.push(error.message));
    await actual.goto('https://www.myasnb.com.my/transactions');
    await actual.waitForFunction(() => window.harnessReady === true);
    await actual.evaluate(({state, context}) => {
      sessionStorage.setItem('isLoggedIn', 'true'); sessionStorage.setItem('UHID', context.account);
      window.dummyState = state;
    }, data);
    const restored = await actual.evaluate(({script, state, account}) =>
      new Function(script)('https://www.myasnb.com.my', state, account),
      {script: data.bridge, state: data.state, account: data.context.account});
    assert.equal(restored, true);
    await actual.getByText('Demo Investor', {exact: true}).waitFor();
    assert.equal(await actual.evaluate(script => new Function(script)(), data.check), true);
    assert.equal(await actual.locator('button').filter({hasText: /^\s*Proceed Payment\s*$/}).isDisabled(), true);
    assert.equal(await actual.getByRole('checkbox').isChecked(), false);
    // Consent interaction is exercised only on this offline dummy component.
    // No live agreement or payment control is clicked.
    await actual.getByRole('checkbox').check();
    assert.equal(await actual.locator('button').filter({hasText: /^\s*Proceed Payment\s*$/}).isDisabled(), false);
    await actual.getByRole('checkbox').uncheck();
    assert.equal(await actual.locator('button').filter({hasText: /^\s*Proceed Payment\s*$/}).isDisabled(), true);
    assert.match(await actual.locator('body').innerText(), /ASM 3/);
    assert.match(await actual.locator('body').innerText(), /5,000.00/);
    assert.equal(await actual.locator('input[type=text]').inputValue(), 'Hong Leong Bank');
    await actual.screenshot({path: path.join(artifacts, 'portal-component-offline.png'), fullPage: true});
    // Expired session cannot even change the browser route.
    const expired = await actual.evaluate(({script, state, account}) => {
      sessionStorage.removeItem('isLoggedIn');
      return new Function(script)('https://www.myasnb.com.my', state, account);
    }, {script: data.bridge, state: data.state, account: data.context.account});
    assert.equal(expired, false);
    assert.deepEqual(errors, []);
    // Run the actual browser-side API dispatch code against a local fetch stub.
    // The response never leaves this isolated context; no investment API is contacted.
    const api = await context.newPage();
    await api.goto('https://www.myasnb.com.my/portfolio');
    await api.waitForFunction(() => window.harnessReady === true);
    await api.evaluate(({context, response}) => {
      document.body.innerHTML = '<p>Authenticated portfolio. No purchase form.</p>';
      sessionStorage.setItem('isLoggedIn', 'true'); sessionStorage.setItem('UHID', context.account);
      history.replaceState(null, '', '/portfolio');
      window.mockCalls = [];
      window.fetch = async (url, options) => {
        window.mockCalls.push({url, options});
        return {status:200,text:async()=>JSON.stringify(response)};
      };
    }, data);
    assert.equal(await api.locator('input,select,form').count(), 0);
    const dispatched = await api.evaluate(({script, context}) => new Promise(resolve =>
      new Function(script)(context, '', resolve)), {script:data.submit,context:data.context});
    assert.deepEqual(dispatched.response, data.response);
    const calls = await api.evaluate(() => window.mockCalls);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].options.method, 'POST');
    assert.equal(calls[0].options.redirect, 'error');
    assert.equal(JSON.parse(calls[0].options.body).amount, '5000');
    assert.equal(JSON.parse(calls[0].options.body).CRITERIACONFIRM, true);
    assert.equal(JSON.parse(calls[0].options.body).PROSPECTUSAGREE, true);
    await api.evaluate(() => sessionStorage.removeItem('isLoggedIn'));
    const stale = await api.evaluate(({script, context}) => new Promise(resolve =>
      new Function(script)(context, '', resolve)), {script:data.submit,context:data.context});
    assert.equal(stale.error, 'context_changed');
    assert.equal(await api.evaluate(() => window.mockCalls.length), 1);
    assert.deepEqual(unexpected, []);
    const result = {preview: 'passed', failureCases: cases,
      actualPortalComponentAndRouter: 'passed with dummy state and stubbed surroundings',
      expiredSessionBridge: 'rejected', browserApiDispatch: 'passed with local fetch stub',
      expiredSessionDispatch: 'rejected before fetch', purchaseFormRequired: false,
      initialAgreementFlags: true, unexpectedBrowserRequests: unexpected,
      livePurchaseRequests: 0, liveFPXRequests: 0};
    fs.writeFileSync(path.join(artifacts, 'browser-results.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally {
    await context.close(); await browser.close();
  }
}

function harness() {
  const source = fs.readFileSync(path.join(artifacts, 'index-5o-fmRqO.js'), 'utf8');
  const component = source.slice(source.indexOf(',Y0=') + 1, source.indexOf(',q0='));
  assert.ok(component.startsWith('Y0=s=>'));
  return `
    import {N as e,b as l} from './vendor-Bu_9jDcs.js';
    import {c as dom} from './vendor-react-dom-BZhdvwsW.js';
    import {B as BrowserRouter,b as Route,u as es} from './vendor-router-CqiASfpO.js';
    const text = {'PORTFOLIO.1':'Fund Name','PROVISION.1':'Name','UHID.1':'UnitHolder ID',
      'PROVISION.3':'FPX Bank','PROVISION.10':'Cancel','PROVISION.11':'Proceed Payment',
      'ADDINVEST.20':'I have read, understood and agreed to the','FOOTER.1':'Terms & Conditions'};
    const $ = () => ({t:k => text[k] || k});
    const gs = () => ({handleSubmit:fn=>fn});
    const ya = () => {}; const Zs = () => null; const Ce = () => null;
    const jt = n => Number(n).toLocaleString('en-MY',{minimumFractionDigits:2});
    const ${component};
    dom.createRoot(document.getElementById('root')).render(e.jsx(BrowserRouter, {children:
      e.jsx(Route, {path:'/paymentAddInv',render:props => e.jsx(Y0, {...props})})}));
    setTimeout(()=>window.harnessReady=true, 100);
  `;
}
main().catch(error => {console.error(error); process.exitCode = 1;});
