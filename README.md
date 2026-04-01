<p align="center">
  <img src="logo.jpg" alt="ASNB Logo" width="200" height="200">
</p>

# MyASNB Buyer Automation

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/yenwee)

Automates ASNB fund unit purchases through the MyASNB portal. Handles login, fund selection, purchase submission, and retries -- then pauses at the FPX payment page for you to complete the bank transfer manually.

> **Disclaimer:** Use at your own risk. Website structures can change and break the script. The script pauses at the payment page -- you must complete payment manually. We are not responsible for any financial loss or account issues.

## Features

- Multi-account support -- run multiple profiles simultaneously
- GUI launcher with per-account tabs and live log output
- Configurable funds, amounts, and banks per profile
- Retry on "insufficient units" (068), skip on "blocked" (1001)
- Session auto-refresh to prevent stale portfolio data
- Email notifications with screenshots on successful purchase
- Direct URL logout for reliable session cleanup
- Debug snapshots on failures for troubleshooting

## Quick Start

```bash
# 1. Setup
make setup

# 2. Edit config.ini -- add your profile(s)
#    See config.ini.template for all options

# 3. Run (pick one)
make run P=yenwee          # CLI - single profile
make gui                   # GUI - all profiles with tabs
```

## Purchase Flow

```mermaid
flowchart TD
    A[Start] --> B[Login to MyASNB]
    B --> C{Active Session?}
    C -->|Yes| D[Wait 90s, retry]
    C -->|No| E[Go to Portfolio]
    D --> E

    E --> F[Select Fund]
    F --> G[Enter Amount + Bank]
    G --> H[Submit Purchase]

    H --> I{Result?}
    I -->|Success| J[Notify + Pause for Payment]
    I -->|068 Insufficient| K[Retry up to 30x]
    K --> H
    I -->|1001 Blocked| L[Try Next Fund]
    L --> F

    J --> M[Complete FPX Payment Manually]
```

## Configuration

All settings live in `config.ini`. Copy from template:

```bash
cp config.ini.template config.ini
```

### Profile Setup

Each `[Profile.xxx]` section is one MyASNB account:

```ini
[Profile.yenwee]
username = your_myasnb_username
password = your_myasnb_password
security_phrase = your_phrase
bank_name = Hong Leong Bank        # optional, falls back to [Settings]
purchase_amount = 5000              # optional
funds_to_try = Amanah Saham Malaysia, Amanah Saham Malaysia 2  # optional
recipient_email = you@email.com    # optional
```

### Global Defaults

`[Settings]` provides defaults that profiles can override:

```ini
[Settings]
funds_to_try = Amanah Saham Malaysia 2
purchase_amount = 100
bank_name = Public Bank
loop_tries = 0                      # 0 = infinite
session_refresh_interval = 6        # logout/re-login every N fund attempts
```

### Available Banks

Affin Bank, Alliance Bank, AmBank, Bank Islam, Bank Rakyat, Bank Muamalat, CIMB Clicks, Hong Leong Bank, HSBC Bank, Maybank2U, Public Bank, RHB Bank, UOB Bank, BSN, KFH, Maybank2E, OCBC Bank, Standard Chartered, AGRONet, Bank of China

### Email Notifications (Optional)

```ini
[Email]
smtp_server = smtp.gmail.com       # Gmail requires App Password
smtp_port = 587
sender_email = you@gmail.com
sender_password = your_app_password
recipient_email = you@gmail.com
send_on_success = true
send_on_failure = false
```

## Usage

```bash
# Run a specific profile
make run P=yenwee

# List available profiles
make run

# Launch GUI (all profiles, with Add Profile button)
make gui

# Monitor logs
make tail

# Stop everything
make stop
```

## Architecture

```
asnb/
  main.py       Automation engine (login, purchase, retry loops, session management)
  config.py     Config loading + profile discovery
  driver.py     Chrome/Brave WebDriver setup (ARM64 Mac compatible)
  email.py      SMTP notifications with screenshot attachments
  actions.py    Human-like browser interactions (typing, clicking, scrolling)
  gui.py        ttkbootstrap GUI with multi-account tabs
```

## How It Works

1. **Config**: Loads profile credentials and settings from `config.ini`
2. **WebDriver**: Initializes Chrome (or Brave fallback on macOS) via `webdriver-manager`
3. **Login**: Handles popup dismissal, active session conflicts (90s wait + retry)
4. **Purchase Loop**: For each fund, enters amount, selects bank, retries up to 30x on 068 errors
5. **On Success**: Sends email notification, plays sound, pauses for manual FPX payment
6. **Session Refresh**: Proactive logout/re-login every N attempts to prevent stale data
7. **Resume**: After payment, press Enter (CLI) or click Resume (GUI) to continue with remaining funds

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Login fails | Check username/password in config.ini. Active session clears in ~1 min. |
| Element not found | Website may have changed. Check debug_snapshots/ for screenshots. |
| WebDriver error | Ensure Chrome/Brave installed. Delete `~/.wdm` to force fresh driver. |
| Bank not found | Check exact spelling in config.ini (case-sensitive, e.g., `Maybank2U`). |
| Email not sending | Use App Password for Gmail. Check smtp settings and spam folder. |
| Funds not loading | Session refresh triggers automatically. Restart if persistent. |

## Prerequisites

- Python 3.11+
- Google Chrome (primary) or Brave Browser (macOS fallback)
- macOS, Linux, or Windows

## Contributing

Contributions welcome! Please open issues for bugs or feature suggestions.

## License

MIT License -- see `LICENSE` for details.
