"""Passive, bounded Chrome telemetry. Never replay or modify requests."""

from collections import OrderedDict, Counter
import hashlib
import json
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
from urllib.parse import urlsplit


class PrivateCaptureHandler(RotatingFileHandler):
    """Every newly created capture file is readable only by its owner."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')


def purchase_response_summary(body):
    """Persist only known business codes/statuses and message classification."""
    result = {'codes': [], 'statuses': [], 'outcome': 'UNKNOWN'}
    messages = []
    def walk(value, depth=0):
        if depth > 5:
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:100]:
                normalized = str(key).upper().replace('_', '')
                if normalized in ('CODE', 'ERRORCODE', 'REJECTCODE') and str(child) in ('068', '68', '1001'):
                    result['codes'].append(str(child))
                if normalized in ('STATUS', 'TRANSACTIONSTATUS') and str(child).upper() in (
                        'SUCCESS', 'SUCCESSFUL', 'PENDING', 'FAILED', 'REJECTED', 'PROCESSING'):
                    result['statuses'].append(str(child).upper())
                if normalized in ('MESSAGE', 'ERROR', 'ERRORS', 'REJECTREASON', 'REASON') and isinstance(child, str):
                    messages.append(child[:2000].lower())
                if isinstance(child, (dict, list)):
                    walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:100]:
                walk(child, depth + 1)
    walk(body)
    text = ' '.join(messages)
    if '1001' in result['codes'] or 'blocked due' in text or 'too many retr' in text:
        result['outcome'] = 'BLOCKED'
    elif any(c in result['codes'] for c in ('068', '68')) or 'insufficient' in text:
        result['outcome'] = 'INSUFFICIENT'
    result['codes'] = sorted(set(result['codes']))
    result['statuses'] = sorted(set(result['statuses']))
    return result


class NetworkTrace:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3)
        self.logger = logging.Logger('network-trace')
        self.logger.addHandler(self.handler)
        self.capture_handler = PrivateCaptureHandler(
            str(path) + '.purchase-private.jsonl', maxBytes=1024 * 1024, backupCount=3, delay=True)
        self.capture_logger = logging.Logger('purchase-private')
        self.capture_logger.addHandler(self.capture_handler)
        self.pending = OrderedDict()
        self.counts = Counter()
        self.last_poll = 0
        self.closed = False

    def drain(self, driver, force=False):
        if self.closed:
            return
        now = time.monotonic()
        if not force and now - self.last_poll < 1:
            return
        self.last_poll = now
        try:
            entries = driver.get_log('performance')
        except Exception:
            self.counts['collection_errors'] += 1
            return
        for entry in entries:
            try:
                message = json.loads(entry['message'])['message']
                if message['method'] == 'Network.loadingFinished':
                    self.capture_purchase(driver, message.get('params', {}).get('requestId'))
                self.consume(message['method'], message.get('params', {}))
            except (ValueError, KeyError, TypeError):
                self.counts['parse_errors'] += 1

    def consume(self, method, p):
        request_id = p.get('requestId')
        if method == 'Network.requestWillBeSent':
            request = p['request']
            url = urlsplit(request['url'])
            # No URLs, request headers, payloads, credentials, or query strings retained.
            hostname = url.hostname or ''
            relevant = p.get('type') in ('XHR', 'Fetch') and (
                hostname == 'myasnb.com.my' or hostname.endswith('.myasnb.com.my'))
            item = {'started': p['timestamp'], 'relevant': relevant}
            if (hostname == 'myasnb-api-v4.myasnb.com.my' and request.get('method') == 'POST'
                    and re.fullmatch(r'/v2/subscription/provisional/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+', url.path)):
                item['purchase'] = True
                payload = request.get('postData', '')
                item['purchase_payload'] = payload if isinstance(payload, str) and len(payload) <= 65536 else None
                try:
                    payload = request.get('postData', '')
                    data = json.loads(payload) if len(payload) <= 65536 else {}
                    allowed = {'amount', 'fpxBankId', 'TXN_CODE', 'referral', 'referralCode',
                               'agree', 'consent', 'risk', 'riskProfile'}
                    item['request_fields'] = sorted(k for k in data if k in allowed) if isinstance(data, dict) else []
                except (ValueError, TypeError):
                    item['request_fields'] = []
            if relevant:
                item.update(endpoint=hashlib.sha256((hostname + url.path).encode()).hexdigest()[:16],
                            method=request.get('method') if request.get('method') in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS') else 'OTHER')
            if request_id in self.pending:
                self.counts['redirects'] += 1
            self.pending[request_id] = item
            self.counts['requests'] += 1
            if relevant:
                self.counts['asnb_api_requests'] += 1
            if len(self.pending) > 512:
                self.pending.popitem(last=False)
                self.counts['evicted_requests'] += 1
        elif method == 'Network.responseReceived':
            item = self.pending.get(request_id)
            if item is None:
                return
            response = p['response']
            status = int(response['status'])
            self.counts[f'http_{status // 100}xx'] += 1
            item['status'] = status
            # Only persist a numeric Retry-After duration, never arbitrary header text.
            headers = response.get('headers', {})
            retry = next((str(v) for k, v in headers.items() if k.lower() == 'retry-after'), '')
            if retry.isdigit() and len(retry) <= 8:
                item['retry_after_seconds'] = int(retry)
        elif method in ('Network.loadingFinished', 'Network.loadingFailed'):
            item = self.pending.pop(request_id, None)
            # Chrome's encodedDataLength is received transfer size, not total network traffic.
            self.counts['received_bytes'] += max(0, int(p.get('encodedDataLength', 0)))
            if method.endswith('Failed'):
                self.counts['blocked_assets' if p.get('blockedReason') == 'inspector'
                            else 'failed_requests'] += 1
            if item and item['relevant']:
                record = {k: v for k, v in item.items() if k not in ('started', 'relevant', 'purchase', 'request_fields', 'purchase_payload')}
                record.update(event='api_response', time=time.time(), duration_ms=round(1000 * max(0, p['timestamp'] - item['started'])),
                              failed=method.endswith('Failed'))
                self.logger.info(json.dumps(record))

    def capture_purchase(self, driver, request_id):
        item = self.pending.get(request_id)
        if not item or not item.get('purchase'):
            return
        record = {'event': 'purchase_response_redacted', 'time': time.time(),
                  'endpoint': item.get('endpoint'), 'method': 'POST', 'http_status': item.get('status'),
                  'request_fields': item.get('request_fields', []),
                  'route': '/v2/subscription/provisional/:account/:fund'}
        try:
            response = driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': request_id})
            body = response.get('body', '')
            if response.get('base64Encoded') or not isinstance(body, str) or len(body) > 65536:
                raise ValueError()
            self.capture_logger.info(json.dumps({
                'event': 'purchase_exchange_private', 'time': record['time'],
                'route': record['route'], 'endpoint': record['endpoint'],
                'method': 'POST', 'http_status': item.get('status'),
                'request_body': item.get('purchase_payload'), 'response_body': body,
            }))
            record.update(purchase_response_summary(json.loads(body)))
        except Exception:
            record['capture'] = 'unavailable'
        self.logger.info(json.dumps(record))

    def outcome(self, stage, outcome):
        # Business codes come from the visible UI, never saved response bodies.
        if outcome not in ('SUCCESS', 'BLOCKED', 'INSUFFICIENT', 'AMOUNT', 'CLASS', 'UNCERTAIN'):
            return
        self.logger.info(json.dumps({'event': 'ui_outcome', 'stage': stage, 'outcome': outcome,
                                     'time': time.time()}))

    def summary(self):
        return dict(self.counts)

    def close(self):
        self.closed = True
        self.handler.close()
        self.capture_handler.close()


def trace_network(driver, force=False):
    trace = getattr(driver, '_asnb_network_trace', None)
    if isinstance(trace, NetworkTrace):
        trace.drain(driver, force=force)


def trace_outcome(driver, stage, outcome):
    trace = getattr(driver, '_asnb_network_trace', None)
    if isinstance(trace, NetworkTrace):
        trace_network(driver, force=True)
        trace.outcome(stage, outcome)


def network_summary(driver):
    trace = getattr(driver, '_asnb_network_trace', None)
    return trace.summary() if isinstance(trace, NetworkTrace) else {}
