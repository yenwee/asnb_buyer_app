'use strict';

// All identities and FPX values are dummy. Only fund, amount and bank match the capture.
const fpxKeys = ['fpx_buyerAccNo', 'fpx_buyerBankBranch', 'fpx_buyerBankId',
  'fpx_buyerEmail', 'fpx_buyerIban', 'fpx_buyerId', 'fpx_buyerName', 'fpx_makerName',
  'fpx_msgToken', 'fpx_msgType', 'fpx_productDesc', 'fpx_sellerBankCode', 'fpx_sellerExId',
  'fpx_sellerExOrderNo', 'fpx_sellerId', 'fpx_sellerOrderNo', 'fpx_sellerTxnTime',
  'fpx_txnAmount', 'fpx_txnCurrency', 'fpx_version', 'fpx_checkSum'];
const fixture = {
  data: {
    TRANSACTIONSTATUS: 'Successful', FUNDID: 'AS1M', FUNDCLASS: 'A',
    AMOUNTAPPLIED: 5000, UNITHOLDERID: 'DEMO-HOLDER', FIRSTNAME: 'Demo Investor',
    TRANSACTIONNUMBER: 'DEMO-TRANSACTION', TRANSACTIONDATE: '2026-10-18',
    TRANSACTIONTIME: '12:07:04', FPX_URL: 'https://example.invalid/disabled',
    FPX_DATA: Object.assign(Object.fromEntries(fpxKeys.map(k => [k, 'DUMMY'])), {
      fpx_buyerBankId: 'DEMO-BANK', fpx_txnAmount: '5000.00', fpx_txnCurrency: 'MYR',
      fpx_checkSum: 'DUMMY-NOT-A-SIGNATURE', fpx_buyerName: 'Demo Investor'
    })
  }, errors: {}
};
const context = {account: 'DEMO-HOLDER', fund_id: 'AS1M', fundshortname: 'ASM 3',
  bankid: 'DEMO-BANK', bankname: 'Hong Leong Bank', amount: '5000.00', isFixed: 1};

function renderState(state, sessionValid = true) {
  const error = document.querySelector('#error');
  const panel = document.querySelector('#confirmation');
  error.hidden = true;
  panel.hidden = true;
  document.querySelector('#agreement').checked = false;
  document.querySelector('#agreement').disabled = true;
  document.querySelector('#payment').disabled = true;
  try {
    if (!sessionValid) throw new Error('Session expired. Sign in manually; no submission or retry.');
    if (!state) throw new Error('Missing navigation state. No submission or retry.');
    if (!state.FPX_URL || fpxKeys.some(k => typeof state[k] !== 'string'))
      throw new Error('Missing FPX fields. No submission or retry.');
    for (const k of ['fundshortname', 'bankname', 'FIRSTNAME', 'UNITHOLDERID', 'TRANSACTIONNUMBER'])
      if (typeof state[k] !== 'string' || !state[k]) throw new Error('Malformed confirmation state. No submission or retry.');
    if (!(Number(state.AMOUNTAPPLIED) > 0) || !Number.isFinite(Number(state.AMOUNTAPPLIED)))
      throw new Error('Malformed amount. No submission or retry.');
    const values = {fund: `${state.fundshortname} (Class ${state.fundClass})`,
      name: state.FIRSTNAME, holder: state.UNITHOLDERID,
      amount: `RM${Number(state.AMOUNTAPPLIED).toLocaleString('en-MY', {minimumFractionDigits: 2})}`,
      reference: state.TRANSACTIONNUMBER, bank: state.bankname};
    for (const [id, value] of Object.entries(values)) document.getElementById(id).textContent = value;
    panel.hidden = false;
    return true;
  } catch (e) {
    error.textContent = e.message;
    error.hidden = false;
    return false;
  }
}

function renderResponse(response, ctx = context, sessionValid = true) {
  if (!response || typeof response.data !== 'object' || response.data === null ||
      response.data.TRANSACTIONSTATUS !== 'Successful' || !ctx) {
    renderState(null, sessionValid);
    document.querySelector('#error').textContent = 'Malformed subscription response. No submission or retry.';
    return false;
  }
  return renderState({...response.data, ...response.data.FPX_DATA,
    fundshortname: ctx.fundshortname, bankname: ctx.bankname, fundClass: response.data.FUNDCLASS}, sessionValid);
}

function runScenario(name) {
  const response = structuredClone(fixture);
  if (name === 'missing-state') return renderState(null);
  if (name === 'missing-fpx') delete response.data.FPX_DATA;
  if (name === 'malformed') return renderResponse({data: 'invalid'});
  return renderResponse(response, context, name !== 'expired');
}

// Extra guards: CSP blocks connections and forms even if code is later changed.
window.fetch = () => Promise.reject(new Error('Offline preview: network blocked'));
window.XMLHttpRequest = class {constructor() {throw new Error('Offline preview: network blocked');}};
navigator.sendBeacon = () => false;
window.WebSocket = class {constructor() {throw new Error('Offline preview: network blocked');}};
window.open = () => null;
document.addEventListener('submit', event => {event.preventDefault(); event.stopImmediatePropagation();}, true);
document.querySelector('#scenario').addEventListener('change', event => runScenario(event.target.value));
window.preview = {runScenario, renderState, renderResponse, fixture, context};
runScenario('success');
