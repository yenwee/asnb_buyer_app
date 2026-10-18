// Isolated Chrome: every URL intercepted; no request reaches MyASNB or FPX.
const fs = require('fs');
const assert = require('assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const source = fs.readFileSync('asnb/browser_transport.py', 'utf8');
const script = source.match(/SUBSCRIPTION_SCRIPT = r"""([\s\S]*?)"""/)[1];
const readiness = source.match(/PORTAL_READY_SCRIPT = r"""([\s\S]*?)"""/)[1];

(async () => {
    const browser = await chromium.launch({channel:'chrome', headless:true});
    const context = await browser.newContext({serviceWorkers:'block'});
    let posts = 0, unexpected = 0, reply = 'reject';
    await context.route('**/*', async route => {
        const request = route.request();
        if (request.url() === 'https://www.myasnb.com.my/portfolio' && request.method() === 'GET') {
            return route.fulfill({contentType:'text/html', body:'<!doctype html><title>Offline transport test</title>'});
        }
        if (request.url() === 'https://myasnb-api-v4.myasnb.com.my/v2/portfolio/DEMO') {
            assert.equal(request.method(), 'GET');
            return route.fulfill({contentType:'application/json', body:'{"data":[]}',
                headers:{'access-control-allow-origin':'https://www.myasnb.com.my',
                         'access-control-allow-credentials':'true',
                         'timing-allow-origin':'https://www.myasnb.com.my'}});
        }
        if (request.url() === 'https://myasnb-api-v4.myasnb.com.my/v2/subscription/provisional/DEMO/AS1M') {
            if (request.method() === 'OPTIONS') {
                return route.fulfill({status:204, headers:{
                    'access-control-allow-origin':'https://www.myasnb.com.my',
                    'access-control-allow-credentials':'true',
                    'access-control-allow-methods':'POST',
                    'access-control-allow-headers':'content-type'}});
            }
            assert.equal(request.method(), 'POST');
            posts++;
            assert.deepEqual(request.postDataJSON(), {amount:'5000',fpxBankId:'DUMMY'});
            return route.fulfill({status:reply === '524' ? 524 : 200,
                headers:{'access-control-allow-origin':'https://www.myasnb.com.my',
                         'access-control-allow-credentials':'true'},
                contentType:reply === '524' ? 'text/html' : 'application/json',
                body:reply === '524' ? '<p>Offline gateway timeout</p>' :
                    '{"data":{"TRANSACTIONSTATUS":"Reject","REJECTCODE":"068"}}'});
        }
        unexpected++;
        return route.abort();
    });
    try {
        const page = await context.newPage();
        await page.goto('https://www.myasnb.com.my/portfolio');
        const call = () => page.evaluate(script => new Promise(resolve => {
            new Function(script).apply(null, ['DEMO','AS1M',
                {amount:'5000',fpxBankId:'DUMMY'},2,resolve]);
        }), script);
        assert.equal((await call()).error, 'context_changed');
        assert.equal(posts, 0);
        await page.evaluate(() => {
            sessionStorage.setItem('isLoggedIn','true');
            sessionStorage.setItem('UHID','DEMO');
        });
        const isReady = () => page.evaluate(script => new Function(script)('DEMO'), readiness);
        assert.equal(await isReady(), false); // Valid auth flags alone are insufficient.
        await page.evaluate(() => fetch('https://myasnb-api-v4.myasnb.com.my/v2/portfolio/DEMO',
            {credentials:'include'}).then(response => response.json()));
        assert.equal(await isReady(), true);
        const rejected = await call();
        assert.equal(rejected.status, 200);
        assert.equal(JSON.parse(rejected.text).data.REJECTCODE, '068');
        assert.equal(posts, 1);
        reply = '524';
        assert.equal((await call()).status, 524);
        assert.equal(posts, 2); // Exactly one fetch per explicit test invocation.
        await page.evaluate(() => sessionStorage.setItem('UHID','OTHER'));
        assert.equal((await call()).error, 'context_changed');
        assert.equal(posts, 2);
        assert.equal(unexpected, 0);
        console.log('PASS: portal readiness barrier, actual Chrome fetch, 068/524 responses, session guards; all network intercepted.');
    } finally {
        await browser.close();
    }
})().catch(error => {console.error(error);process.exitCode = 1;});
