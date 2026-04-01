#!/usr/bin/env python3
"""ASNB Buyer GUI - ttkbootstrap launcher with multi-account support."""

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
import configparser
import subprocess
import threading
import signal
import sys
import os
from pathlib import Path
from datetime import datetime

from asnb.config import load_config, get_profiles, ConfigError

CONFIG_FILE = "config.ini"
CONFIG_TEMPLATE = "config.ini.template"

FUNDS = [
    "Amanah Saham Malaysia",
    "Amanah Saham Malaysia 2",
    "Amanah Saham Malaysia 3",
]

BANKS = [
    "Affin Bank", "Alliance Bank", "AmBank", "Bank Islam", "Bank Rakyat",
    "Bank Muamalat", "CIMB Clicks", "Hong Leong Bank", "HSBC Bank",
    "Maybank2U", "Public Bank", "RHB Bank", "UOB Bank", "BSN", "KFH",
    "Maybank2E", "OCBC Bank", "Standard Chartered", "AGRONet", "Bank of China",
]

AMOUNTS = ["100", "200", "500", "1000", "2000", "5000"]


class AccountRunner:
    """Manages a single account's subprocess and log output."""

    RESUME_MARKER = "[RESUME_READY]"

    def __init__(self, profile_key, profile_data, log_widget, status_label, root,
                 resume_btn=None):
        self.profile_key = profile_key
        self.profile_data = profile_data
        self.log_widget = log_widget
        self.status_label = status_label
        self.root = root
        self.resume_btn = resume_btn
        self.process = None
        self.log_thread = None

    def start(self):
        if self.process:
            return

        python_path = str(Path(".venv/bin/python"))
        if not Path(python_path).exists():
            python_path = sys.executable

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [python_path, "-u", "-m", "asnb.main", "--profile", self.profile_key],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            preexec_fn=os.setsid,
            env=env,
        )

        self.status_label.configure(text="  RUNNING  ", bootstyle="inverse-success")
        self.log(f"Started ({self.profile_data.get('username', self.profile_key)})")

        self.log_thread = threading.Thread(target=self._read_output, daemon=True)
        self.log_thread.start()

    def stop(self):
        proc = self.process
        if not proc:
            return
        self.process = None
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            self.log("Stopping (waiting for logout)...")
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                self.log("Force stopped.")
        except (ProcessLookupError, OSError):
            pass
        self.status_label.configure(text="  STOPPED  ", bootstyle="inverse-danger")
        self.log("Stopped.")

    @property
    def is_running(self):
        return self.process is not None

    def resume(self):
        proc = self.process
        if proc and proc.stdin:
            if self.resume_btn:
                self.resume_btn.configure(state="disabled")
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
                self.log("Resume signal sent. Continuing automation...")
                self.status_label.configure(text="  RUNNING  ", bootstyle="inverse-success")
                if self.resume_btn:
                    self.resume_btn.pack_forget()
            except (OSError, BrokenPipeError):
                self.log("Failed to send resume signal.")

    def _read_output(self):
        proc = self.process
        if not proc:
            return
        try:
            for line in proc.stdout:
                line = line.rstrip('\n')
                if line:
                    if self.RESUME_MARKER in line:
                        self.root.after(0, self._show_resume)
                    else:
                        self.root.after(0, self.log, line)
            return_code = proc.wait()
            self.root.after(0, self.log, f"Process exited (code {return_code})")
            self.root.after(0, self._on_end)
        except (AttributeError, OSError):
            self.root.after(0, self._on_end)
        except Exception as e:
            self.root.after(0, self.log, f"Error: {e}")
            self.root.after(0, self._on_end)

    def _show_resume(self):
        self.status_label.configure(text="  PAYMENT  ", bootstyle="inverse-warning")
        self.log("Payment ready! Complete payment in browser, then click Resume.")
        if self.resume_btn:
            self.resume_btn.pack(side=RIGHT, padx=(5, 0))

    def _on_end(self):
        self.process = None
        self.status_label.configure(text="  STOPPED  ", bootstyle="inverse-danger")
        if self.resume_btn:
            self.resume_btn.pack_forget()

    MAX_LOG_LINES = 2000

    def log(self, message):
        self.log_widget.configure(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_widget.insert("end", f"[{timestamp}] {message}\n")
        line_count = int(self.log_widget.index("end-1c").split(".")[0])
        if line_count > self.MAX_LOG_LINES:
            self.log_widget.delete("1.0", f"{line_count - self.MAX_LOG_LINES}.0")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")


class ASNBApp:
    def __init__(self):
        self.root = ttk.Window(
            title="ASNB Buyer",
            themename="superhero",
            size=(820, 850),
            minsize=(700, 700),
        )

        self.runners = {}
        self.profiles = {}
        # Per-profile UI state: {profile_key: {bank_var, amount_var, fund_vars, email_var}}
        self.tab_vars = {}

        self._discover_profiles()
        self._build_ui()
        self._load_config_to_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _discover_profiles(self):
        try:
            config = load_config()
            self.profiles = get_profiles(config)
        except ConfigError:
            self.profiles = {}

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=20)
        main.pack(fill=BOTH, expand=YES)
        self._main_frame = main

        # --- Header ---
        header = ttk.Frame(main)
        header.pack(fill=X, pady=(0, 15))

        ttk.Label(header, text="ASNB Buyer",
                  font=("-size", 24, "-weight", "bold"),
                  bootstyle="inverse-primary",
                  padding=(12, 6)).pack(side=LEFT)

        ttk.Button(header, text="Add Profile", bootstyle="outline-success",
                   command=self._show_add_profile_dialog).pack(side=RIGHT, ipady=3)

        # --- Master Controls ---
        ctrl_frame = ttk.Frame(main)
        ctrl_frame.pack(fill=X, pady=(0, 12))

        ttk.Button(ctrl_frame, text="Start All", bootstyle="success", width=12,
                   command=self._start_all).pack(side=LEFT, padx=(0, 6), ipady=5)
        ttk.Button(ctrl_frame, text="Stop All", bootstyle="danger", width=10,
                   command=self._stop_all).pack(side=LEFT, padx=(0, 6), ipady=5)
        ttk.Button(ctrl_frame, text="Save Config", bootstyle="outline-info", width=12,
                   command=self._save_config).pack(side=LEFT, ipady=5)

        # --- Account Tabs ---
        self.notebook = ttk.Notebook(main, bootstyle="info")
        self.notebook.pack(fill=BOTH, expand=YES)

        self._build_account_tabs()

    def _build_account_tabs(self):
        if not self.profiles:
            self._build_empty_state()
            return

        # Load config once for reading defaults
        config = configparser.ConfigParser()
        if Path(CONFIG_FILE).exists():
            config.read(CONFIG_FILE)
        default_bank = config.get('Settings', 'bank_name', fallback='Public Bank')
        default_amount = config.get('Settings', 'purchase_amount', fallback='500')
        default_funds_str = config.get('Settings', 'funds_to_try', fallback='')
        default_funds = [f.strip() for f in default_funds_str.split(',') if f.strip()]

        for profile_key, profile_data in self.profiles.items():
            tab = ttk.Frame(self.notebook, padding=8)
            tab_label = profile_key.title()
            self.notebook.add(tab, text=f"  {tab_label}  ")

            # --- Tab header with status + controls ---
            tab_header = ttk.Frame(tab)
            tab_header.pack(fill=X, pady=(0, 6))

            status = ttk.Label(tab_header, text="  STOPPED  ",
                               font=("-size", 11, "-weight", "bold"),
                               bootstyle="inverse-danger", padding=(8, 3))
            status.pack(side=LEFT, padx=(0, 10))

            ttk.Label(tab_header, text=f"({profile_data.get('username', '?')})",
                      font=("-size", 11), bootstyle="secondary").pack(side=LEFT)

            stop_btn = ttk.Button(tab_header, text="Stop", bootstyle="outline-danger", width=6,
                                   command=lambda k=profile_key: self.runners[k].stop())
            stop_btn.pack(side=RIGHT, padx=(5, 0))

            start_btn = ttk.Button(tab_header, text="Start", bootstyle="outline-success", width=6,
                                    command=lambda k=profile_key: self._start_one(k))
            start_btn.pack(side=RIGHT)

            resume_btn = ttk.Button(tab_header, text="Resume", bootstyle="warning", width=8,
                                     command=lambda k=profile_key: self.runners[k].resume())

            ttk.Button(tab_header, text="Clear", bootstyle="outline-secondary", width=5,
                       command=lambda t=tab: self._clear_tab_log(t)).pack(side=RIGHT, padx=(0, 8))

            # --- Per-profile settings ---
            settings_frame = ttk.Labelframe(tab, text="Settings", padding=8, bootstyle="info")
            settings_frame.pack(fill=X, pady=(0, 6))

            # Bank + Amount row
            row1 = ttk.Frame(settings_frame)
            row1.pack(fill=X, pady=(0, 5))

            ttk.Label(row1, text="Bank", font=("-size", 11), width=7).pack(side=LEFT)
            bank_var = ttk.StringVar()
            ttk.Combobox(row1, textvariable=bank_var, values=BANKS, state="readonly",
                         font=("-size", 11), bootstyle="info", width=20).pack(side=LEFT, padx=(0, 15))

            ttk.Label(row1, text="Amount", font=("-size", 11), width=7).pack(side=LEFT)
            amount_var = ttk.StringVar()
            ttk.Entry(row1, textvariable=amount_var,
                      font=("-size", 12, "-weight", "bold"),
                      bootstyle="info", width=7).pack(side=LEFT, padx=(0, 6))

            for amt in AMOUNTS:
                ttk.Button(row1, text=amt, width=5, bootstyle="outline-info",
                           command=lambda a=amt, v=amount_var: v.set(a)).pack(side=LEFT, padx=1)

            # Funds row
            row2 = ttk.Frame(settings_frame)
            row2.pack(fill=X, pady=(0, 3))

            ttk.Label(row2, text="Funds", font=("-size", 11), width=7).pack(side=LEFT)
            fund_vars = {}
            for fund in FUNDS:
                short = fund.replace("Amanah Saham Malaysia", "ASM").strip() or "ASM 1"
                var = ttk.BooleanVar(value=True)
                fund_vars[fund] = var
                ttk.Checkbutton(row2, text=short, variable=var,
                                bootstyle="info-round-toggle").pack(side=LEFT, padx=(0, 12))

            ttk.Button(row2, text="All", width=4, bootstyle="outline-secondary",
                       command=lambda fv=fund_vars: [v.set(True) for v in fv.values()]).pack(side=LEFT, padx=2)
            ttk.Button(row2, text="None", width=5, bootstyle="outline-secondary",
                       command=lambda fv=fund_vars: [v.set(False) for v in fv.values()]).pack(side=LEFT, padx=2)

            # Email row
            row3 = ttk.Frame(settings_frame)
            row3.pack(fill=X)

            ttk.Label(row3, text="Notify", font=("-size", 11), width=7).pack(side=LEFT)
            email_var = ttk.StringVar(value=profile_data.get("recipient_email", ""))
            ttk.Entry(row3, textvariable=email_var, font=("-size", 11),
                      bootstyle="info", width=30).pack(side=LEFT, padx=(0, 5))
            ttk.Label(row3, text="(comma-separated)",
                      font=("-size", 10), bootstyle="secondary").pack(side=LEFT)

            # Set values: profile override > global default
            prof_bank = profile_data.get('bank_name', default_bank)
            prof_amount = profile_data.get('purchase_amount', default_amount)
            prof_funds_str = profile_data.get('funds_to_try', '')
            prof_funds = [f.strip() for f in prof_funds_str.split(',') if f.strip()] if prof_funds_str else default_funds

            bank_var.set(prof_bank)
            amount_var.set(prof_amount)
            for fund, var in fund_vars.items():
                var.set(fund in prof_funds)

            # Store vars for saving
            self.tab_vars[profile_key] = {
                'bank_var': bank_var,
                'amount_var': amount_var,
                'fund_vars': fund_vars,
                'email_var': email_var,
            }

            # --- Log area ---
            log_frame = ttk.Frame(tab)
            log_frame.pack(fill=BOTH, expand=YES)

            log_text = ttk.Text(log_frame, font=("Menlo", 11), wrap="word",
                                state="disabled", bg="#1a1a2e", fg="#c8c8e0",
                                insertbackground="#c8c8e0", selectbackground="#3a3a5e",
                                relief="flat", padx=10, pady=8)
            scrollbar = ttk.Scrollbar(log_frame, orient="vertical",
                                       command=log_text.yview, bootstyle="info-round")
            log_text.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side=RIGHT, fill=Y)
            log_text.pack(side=LEFT, fill=BOTH, expand=YES)

            self.runners[profile_key] = AccountRunner(
                profile_key, profile_data, log_text, status, self.root,
                resume_btn=resume_btn
            )

    def _build_empty_state(self):
        empty = ttk.Frame(self.notebook, padding=30)
        self.notebook.add(empty, text="  No Profiles  ")
        ttk.Label(empty, text="No profiles found in config.ini",
                  font=("-size", 14)).pack(pady=(20, 10))
        ttk.Label(empty, text="Click 'Add Profile' to get started.",
                  font=("-size", 12), bootstyle="secondary").pack()
        ttk.Button(empty, text="Add Profile", bootstyle="success",
                   command=self._show_add_profile_dialog).pack(pady=20, ipady=5)

    def _reload_profiles(self):
        self._stop_all()
        for tab_id in self.notebook.tabs():
            self.notebook.forget(tab_id)
        self.runners.clear()
        self.tab_vars.clear()
        self._discover_profiles()
        self._build_account_tabs()

    def _show_add_profile_dialog(self):
        dialog = ttk.Toplevel(self.root)
        dialog.title("Add Profile")
        dialog.geometry("450x420")
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill=BOTH, expand=YES)

        fields = {}
        for label_text, key, required in [
            ("Profile Name", "name", True),
            ("Username", "username", True),
            ("Password", "password", True),
            ("Security Phrase", "security_phrase", True),
            ("Recipient Email", "recipient_email", False),
            ("Purchase Amount", "purchase_amount", False),
        ]:
            row = ttk.Frame(frame)
            row.pack(fill=X, pady=3)
            lbl = ttk.Label(row, text=f"{label_text}{'*' if required else ''}", width=16)
            lbl.pack(side=LEFT)
            var = ttk.StringVar()
            if key == "password":
                ttk.Entry(row, textvariable=var, show="*", width=30).pack(side=LEFT, fill=X, expand=YES)
            else:
                ttk.Entry(row, textvariable=var, width=30).pack(side=LEFT, fill=X, expand=YES)
            fields[key] = var

        row = ttk.Frame(frame)
        row.pack(fill=X, pady=3)
        ttk.Label(row, text="Bank", width=16).pack(side=LEFT)
        bank_var = ttk.StringVar()
        ttk.Combobox(row, textvariable=bank_var, values=BANKS, state="readonly", width=27).pack(side=LEFT)
        fields["bank_name"] = bank_var

        error_var = ttk.StringVar()
        ttk.Label(frame, textvariable=error_var, bootstyle="danger",
                  font=("-size", 10), wraplength=400).pack(fill=X, pady=(8, 0))

        def save():
            error_var.set("")
            name = fields["name"].get().strip().lower().replace(" ", "_")
            if not name:
                error_var.set("Profile name is required.")
                return
            for key in ["username", "password", "security_phrase"]:
                if not fields[key].get().strip():
                    error_var.set(f"{key.replace('_', ' ').title()} is required.")
                    return

            config = configparser.ConfigParser()
            config.read(CONFIG_FILE)
            section = f"Profile.{name}"
            if config.has_section(section):
                error_var.set(f"Profile '{name}' already exists.")
                return
            config.add_section(section)
            for key, var in fields.items():
                if key == "name":
                    continue
                val = var.get().strip()
                if val:
                    config.set(section, key, val)
            with open(CONFIG_FILE, "w") as f:
                config.write(f)
            dialog.destroy()
            self._reload_profiles()

        ttk.Button(frame, text="Save Profile", bootstyle="success",
                   command=save).pack(pady=(15, 0), ipady=5)

    def _load_config_to_ui(self):
        """Load is handled during tab creation via profile_data + defaults."""
        pass

    def _save_config(self):
        config = configparser.ConfigParser()
        config.read(CONFIG_FILE)

        for profile_key, vars_dict in self.tab_vars.items():
            section = f'Profile.{profile_key}'
            if section not in config:
                continue

            selected_funds = [f for f, v in vars_dict['fund_vars'].items() if v.get()]
            config[section]['funds_to_try'] = ", ".join(selected_funds)
            config[section]['purchase_amount'] = vars_dict['amount_var'].get()
            config[section]['bank_name'] = vars_dict['bank_var'].get()
            config[section]['recipient_email'] = vars_dict['email_var'].get().strip()

        with open(CONFIG_FILE, 'w') as f:
            config.write(f)

        for runner in self.runners.values():
            runner.log("Config saved.")

    def _start_one(self, profile_key):
        vars_dict = self.tab_vars.get(profile_key, {})
        fund_vars = vars_dict.get('fund_vars', {})
        selected = [f for f, v in fund_vars.items() if v.get()]
        if not selected:
            self.runners[profile_key].log("Select at least one fund.")
            return
        amount = vars_dict.get('amount_var', ttk.StringVar()).get()
        if not amount.strip():
            self.runners[profile_key].log("Enter an amount.")
            return

        self._save_config()
        runner = self.runners[profile_key]
        if not runner.is_running:
            bank = vars_dict.get('bank_var', ttk.StringVar()).get()
            runner.log(f"Funds: {', '.join(selected)}")
            runner.log(f"Amount: RM {amount} | Bank: {bank}")
            runner.log("-" * 50)
            runner.start()

    def _start_all(self):
        for key in self.runners:
            self._start_one(key)

    def _stop_all(self):
        for runner in self.runners.values():
            if runner.is_running:
                runner.stop()

    def _clear_tab_log(self, tab):
        for widget in tab.winfo_children():
            for child in widget.winfo_children():
                if isinstance(child, ttk.Text):
                    child.configure(state="normal")
                    child.delete("1.0", "end")
                    child.configure(state="disabled")
                    return

    def _on_close(self):
        self._stop_all()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    import atexit

    app = ASNBApp()

    def _cleanup():
        for runner in app.runners.values():
            proc = runner.process
            if proc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    continue
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass

    atexit.register(_cleanup)
    app.run()
