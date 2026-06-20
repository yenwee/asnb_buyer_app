#!/usr/bin/env python3
"""Small localhost control panel for ASNB Buyer."""

import configparser
import html
import json
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from asnb.config import load_config, get_profiles, ConfigError

CONFIG_FILE = "config.ini"

FUNDS = [
    "Amanah Saham Malaysia",
    "Amanah Saham Malaysia 2 Wawasan",
    "Amanah Saham Malaysia 3",
]

FUND_ALIASES = {
    "Amanah Saham Malaysia 2 Wawasan": {
        "Amanah Saham Malaysia 2",
        "Amanah Saham Malaysia 2 Wawasan",
        "Amanah Saham Malaysia 2 - Wawasan",
        "ASM 2",
        "ASM 2 Wawasan",
        "ASM 2 - Wawasan",
    },
}

BANKS = [
    "Affin Bank", "Alliance Bank", "AmBank", "Bank Islam", "Bank Rakyat",
    "Bank Muamalat", "CIMB Clicks", "Hong Leong Bank", "HSBC Bank",
    "Maybank2U", "Public Bank", "RHB Bank", "UOB Bank", "BSN", "KFH",
    "Maybank2E", "OCBC Bank", "Standard Chartered", "AGRONet", "Bank of China",
]


def fund_selected_by_config(gui_fund, configured_funds):
    if gui_fund in configured_funds:
        return True
    return any(alias in configured_funds for alias in FUND_ALIASES.get(gui_fund, set()))


class WebRunner:
    RESUME_MARKER = "[RESUME_READY]"
    MAX_LOG_LINES = 2000

    def __init__(self, profile_key):
        self.profile_key = profile_key
        self.process = None
        self.status = "stopped"
        self.logs = deque(maxlen=self.MAX_LOG_LINES)
        self._reader = None
        self._lock = threading.Lock()

    def log(self, message):
        with self._lock:
            self.logs.append(message)

    def start(self):
        with self._lock:
            if self.process:
                return
            python_path = Path(".venv/bin/python")
            executable = str(python_path) if python_path.exists() else sys.executable
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            self.process = subprocess.Popen(
                [executable, "-u", "-m", "asnb.main", "--profile", self.profile_key],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                preexec_fn=os.setsid if os.name != "nt" else None,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                env=env,
            )
            self.status = "running"
            self.logs.clear()
            self.logs.append(f"Started profile {self.profile_key}.")
            self._reader = threading.Thread(target=self._read_output, daemon=True)
            self._reader.start()

    def stop(self):
        with self._lock:
            proc = self.process
            if not proc:
                self.status = "stopped"
                return
            self.process = None
            self.status = "stopped"
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            self.log("Stopping. Waiting for logout...")
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                self.log("Force stopped.")
        except (ProcessLookupError, OSError):
            pass

    def resume(self):
        with self._lock:
            proc = self.process
        if proc and proc.stdin:
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
                self.status = "running"
                self.log("Resume signal sent.")
            except (OSError, BrokenPipeError):
                self.log("Failed to send resume signal.")

    def _read_output(self):
        proc = self.process
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            if self.RESUME_MARKER in line:
                with self._lock:
                    self.status = "payment"
                self.log("Payment ready. Complete payment in browser, then resume.")
            else:
                self.log(line)
        code = proc.wait()
        with self._lock:
            if self.process is proc:
                self.process = None
            if self.status != "payment":
                self.status = "stopped"
        self.log(f"Process exited with code {code}.")

    def snapshot(self):
        with self._lock:
            return {
                "status": self.status,
                "running": self.process is not None,
                "logs": list(self.logs),
            }


class AppState:
    def __init__(self):
        self.runners = {}
        self.lock = threading.Lock()

    def get_runner(self, profile_key):
        with self.lock:
            if profile_key not in self.runners:
                self.runners[profile_key] = WebRunner(profile_key)
            return self.runners[profile_key]


STATE = AppState()


def load_profiles():
    config = load_config()
    return config, get_profiles(config)


def get_profile_settings(config, profile_key, profile_data):
    default_bank = config.get("Settings", "bank_name", fallback="Public Bank")
    default_amount = config.get("Settings", "purchase_amount", fallback="500")
    default_referral = config.get("Settings", "referral_code", fallback="")
    default_funds_str = config.get("Settings", "funds_to_try", fallback="")
    default_funds = [f.strip() for f in default_funds_str.split(",") if f.strip()]
    funds_str = profile_data.get("funds_to_try", "")
    profile_funds = [f.strip() for f in funds_str.split(",") if f.strip()] if funds_str else default_funds
    return {
        "bank_name": profile_data.get("bank_name", default_bank),
        "purchase_amount": profile_data.get("purchase_amount", default_amount),
        "referral_code": profile_data.get("referral_code", default_referral),
        "recipient_email": profile_data.get("recipient_email", ""),
        "funds": [fund for fund in FUNDS if fund_selected_by_config(fund, profile_funds)],
    }


def save_profile(profile_key, form):
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    section = f"Profile.{profile_key}"
    if section not in config:
        raise ValueError(f"Profile not found: {profile_key}")

    current_keys = set(config[section].keys())
    default_funds_str = config.get("Settings", "funds_to_try", fallback="")
    default_funds = [f.strip() for f in default_funds_str.split(",") if f.strip()]
    default_ui_funds = [fund for fund in FUNDS if fund_selected_by_config(fund, default_funds)]
    default_values = {
        "funds_to_try": ", ".join(default_ui_funds),
        "purchase_amount": config.get("Settings", "purchase_amount", fallback="500"),
        "bank_name": config.get("Settings", "bank_name", fallback="Public Bank"),
        "referral_code": config.get("Settings", "referral_code", fallback=""),
        "recipient_email": "",
    }

    selected_funds = [fund for fund in FUNDS if fund in form.get("funds", [])]
    values = {
        "funds_to_try": ", ".join(selected_funds),
        "purchase_amount": form.get("purchase_amount", [""])[0].strip(),
        "bank_name": form.get("bank_name", [""])[0].strip(),
        "referral_code": form.get("referral_code", [""])[0].strip(),
        "recipient_email": form.get("recipient_email", [""])[0].strip(),
    }

    changed = False
    for option, value in values.items():
        if option not in current_keys and value == default_values.get(option, ""):
            continue
        if config[section].get(option, "") != value:
            config[section][option] = value
            changed = True

    if changed:
        with open(CONFIG_FILE, "w") as f:
            config.write(f)


def render_page():
    try:
        config, profiles = load_profiles()
        error = ""
    except ConfigError as exc:
        config, profiles, error = None, {}, str(exc)

    cards = []
    for profile_key, profile_data in profiles.items():
        settings = get_profile_settings(config, profile_key, profile_data)
        runner = STATE.get_runner(profile_key).snapshot()
        status = runner["status"]
        start_disabled = "disabled" if status in {"running", "payment"} else ""
        stop_disabled = "disabled" if status == "stopped" else ""
        resume_disabled = "" if status == "payment" else "disabled"
        fund_checks = "\n".join(
            f'<label class="chip"><input type="checkbox" name="funds" value="{html.escape(fund)}" '
            f'{"checked" if fund in settings["funds"] else ""}> <span>{html.escape(fund)}</span></label>'
            for fund in FUNDS
        )
        bank_options = "\n".join(
            f'<option value="{html.escape(bank)}" {"selected" if bank == settings["bank_name"] else ""}>'
            f'{html.escape(bank)}</option>'
            for bank in BANKS
        )
        recent_logs = "\n".join(html.escape(line) for line in runner["logs"][-80:])
        cards.append(f"""
        <section class="profile" data-profile="{html.escape(profile_key)}">
          <div class="profile-head">
            <div>
              <h2>{html.escape(profile_key)}</h2>
              <p>{html.escape(profile_data.get("username", "?"))}</p>
            </div>
            <span class="status status-{html.escape(status)}">{html.escape(status.upper())}</span>
          </div>
          <form method="post" action="/action">
            <input type="hidden" name="profile" value="{html.escape(profile_key)}">
            <div class="grid">
              <label><span>Bank</span><select name="bank_name">{bank_options}</select></label>
              <label><span>Amount</span><input name="purchase_amount" inputmode="numeric" value="{html.escape(settings["purchase_amount"])}"></label>
              <label><span>Referral</span><input name="referral_code" value="{html.escape(settings["referral_code"])}"></label>
              <label><span>Notify</span><input name="recipient_email" value="{html.escape(settings["recipient_email"])}"></label>
            </div>
            <div class="funds">{fund_checks}</div>
            <div class="actions">
              <button class="secondary" name="action" value="save">Save</button>
              <button class="primary" name="action" value="start" {start_disabled}>Start</button>
              <button class="danger" name="action" value="stop" {stop_disabled}>Stop</button>
              <button class="secondary" name="action" value="resume" {resume_disabled}>Resume</button>
            </div>
          </form>
          <div class="log-head"><span>Live Log</span><span>last 80 lines</span></div>
          <pre id="log-{html.escape(profile_key)}">{recent_logs}</pre>
        </section>
        """)

    snapshots = [STATE.get_runner(profile_key).snapshot() for profile_key in profiles]
    running_count = sum(1 for item in snapshots if item["status"] == "running")
    payment_count = sum(1 for item in snapshots if item["status"] == "payment")
    body = "\n".join(cards) if cards else f"<p class='error'>{html.escape(error or 'No profiles found.')}</p>"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ASNB Buyer</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --ink: #17202e;
      --muted: #667085;
      --line: #d9e0ea;
      --soft: #eef3f7;
      --accent: #0f766e;
      --accent-ink: #ffffff;
      --danger: #b42318;
      --warn: #a15c07;
      --log: #101828;
      --log-text: #d0d5dd;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }}
    header {{ border-bottom: 1px solid var(--line); background: var(--panel); }}
    .topbar {{ max-width: 1240px; margin: 0 auto; padding: 18px; display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
    h1 {{ margin: 0; font-size: 24px; font-weight: 800; }}
    .sub {{ margin-top: 3px; color: var(--muted); font-size: 13px; }}
    .metrics {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: end; }}
    .metric {{ min-width: 92px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--soft); }}
    .metric strong {{ display: block; font-size: 18px; line-height: 1; }}
    .metric span {{ display: block; margin-top: 4px; color: var(--muted); font-size: 12px; }}
    main {{ padding: 16px; display: grid; gap: 14px; max-width: 1240px; margin: 0 auto; }}
    .profile {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; box-shadow: 0 1px 2px rgba(16, 24, 40, .04); }}
    .profile-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; padding-bottom: 10px; border-bottom: 1px solid var(--line); }}
    h2 {{ margin: 0; font-size: 18px; font-weight: 750; }}
    p {{ margin: 4px 0 0; color: var(--muted); }}
    .status {{ min-width: 86px; text-align: center; padding: 5px 8px; border-radius: 6px; font-size: 12px; font-weight: 800; background: #e5e7eb; color: #475467; }}
    .status-running {{ background: #ccfbf1; color: #115e59; }}
    .status-payment {{ background: #fef3c7; color: #92400e; }}
    .grid {{ display: grid; grid-template-columns: 1.4fr .7fr .8fr 1.3fr; gap: 10px; margin-top: 12px; }}
    label {{ display: grid; gap: 5px; font-size: 13px; color: var(--muted); }}
    label > span {{ font-weight: 700; }}
    input, select {{ width: 100%; min-height: 38px; padding: 8px 9px; border: 1px solid var(--line); border-radius: 6px; font: inherit; color: var(--ink); background: white; }}
    input:focus, select:focus {{ outline: 2px solid rgba(15, 118, 110, .16); border-color: var(--accent); }}
    .funds {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }}
    .chip {{ display: flex; align-items: center; gap: 7px; min-height: 34px; padding: 6px 9px; border: 1px solid var(--line); border-radius: 8px; background: #fbfcfe; color: var(--ink); }}
    .chip input {{ width: auto; min-height: 0; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    button {{ min-width: 82px; min-height: 38px; padding: 8px 12px; border: 1px solid var(--accent); background: var(--accent); color: var(--accent-ink); border-radius: 6px; font-weight: 800; cursor: pointer; }}
    button.secondary {{ background: #ffffff; color: var(--accent); }}
    button.danger {{ border-color: var(--danger); background: var(--danger); }}
    button:disabled {{ opacity: .45; cursor: not-allowed; }}
    .log-head {{ display: flex; justify-content: space-between; margin-top: 12px; color: var(--muted); font-size: 12px; font-weight: 700; }}
    pre {{ min-height: 210px; max-height: 360px; overflow: auto; padding: 11px; background: var(--log); color: var(--log-text); border-radius: 6px; font-size: 12px; line-height: 1.45; white-space: pre-wrap; }}
    .error {{ color: #b91c1c; }}
    @media (max-width: 820px) {{
      .topbar {{ align-items: stretch; flex-direction: column; }}
      .metrics {{ justify-content: stretch; }}
      .metric {{ flex: 1; }}
      .grid {{ grid-template-columns: 1fr; }}
      button {{ flex: 1; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>ASNB Buyer</h1>
        <div class="sub">Local control panel at 127.0.0.1. Saves config, starts runners, and tails live output.</div>
      </div>
      <div class="metrics">
        <div class="metric"><strong>{len(profiles)}</strong><span>profiles</span></div>
        <div class="metric"><strong>{running_count}</strong><span>running</span></div>
        <div class="metric"><strong>{payment_count}</strong><span>payment</span></div>
      </div>
    </div>
  </header>
  <main>{body}</main>
  <script>
    async function refreshLogs() {{
      for (const section of document.querySelectorAll('.profile')) {{
        const profile = section.dataset.profile;
        const res = await fetch('/logs?profile=' + encodeURIComponent(profile));
        if (!res.ok) continue;
        const data = await res.json();
        const status = section.querySelector('.status');
        status.textContent = data.status.toUpperCase();
        status.className = 'status status-' + data.status;
        section.querySelector('button[value="start"]').disabled = data.status === 'running' || data.status === 'payment';
        section.querySelector('button[value="stop"]').disabled = data.status === 'stopped';
        section.querySelector('button[value="resume"]').disabled = data.status !== 'payment';
        const log = section.querySelector('pre');
        log.textContent = data.logs.join('\\n');
        log.scrollTop = log.scrollHeight;
      }}
    }}
    setInterval(refreshLogs, 2000);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/logs":
            query = parse_qs(parsed.query)
            profile = query.get("profile", [""])[0]
            payload = STATE.get_runner(profile).snapshot()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(render_page().encode("utf-8"))

    def do_POST(self):
        if urlparse(self.path).path != "/action":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        profile = form.get("profile", [""])[0]
        action = form.get("action", [""])[0]
        runner = STATE.get_runner(profile)
        try:
            if action in {"save", "start"}:
                save_profile(profile, form)
            if action == "start":
                runner.start()
            elif action == "stop":
                runner.stop()
            elif action == "resume":
                runner.resume()
            elif action != "save":
                raise ValueError(f"Unknown action: {action}")
        except Exception as exc:
            runner.log(f"Action failed: {exc}")
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def main(host="127.0.0.1", port=8765, open_browser=False):
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"ASNB web UI running at {url}")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for runner in list(STATE.runners.values()):
            runner.stop()
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ASNB Buyer localhost UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the UI in the default browser")
    args = parser.parse_args()
    main(args.host, args.port, open_browser=args.open)
