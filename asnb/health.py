"""Browser-specific RSS measurements and cooldown-aware maintenance policy."""

import subprocess
import time


def browser_rss_mb(driver):
    """Sum RSS for this Chrome process tree (shared pages may be counted twice).

    No command arguments or unrelated browser sessions are read. Unsupported
    platforms/process races return None; missing telemetry never triggers a restart.
    """
    root = driver.capabilities.get('goog:processID')
    if not root:
        process = getattr(getattr(driver, 'service', None), 'process', None)
        root = getattr(process, 'pid', None)
    if not root:
        return None
    try:
        output = subprocess.run(
            ['ps', '-axo', 'pid=,ppid=,rss='], capture_output=True, text=True,
            check=True, timeout=2,
        ).stdout
        rows = [tuple(map(int, row.split())) for row in output.splitlines() if row.strip()]
        owned = {int(root)}
        if not any(pid == int(root) for pid, _, _ in rows):
            return None
        while True:
            children = {pid for pid, parent, _ in rows if parent in owned}
            if children <= owned:
                break
            owned.update(children)
        return sum(rss for pid, _, rss in rows if pid in owned) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class BrowserHealth:
    """Require sustained high RSS, prefer idle windows, bound restart deferral."""

    def __init__(self, limit_mb=3072, sample_seconds=60, max_defer_seconds=300):
        self.limit_mb = limit_mb
        self.sample_seconds = sample_seconds
        self.max_defer_seconds = max_defer_seconds
        self.reset()

    def reset(self):
        self.last_sample = None
        self.high_samples = 0
        self.pending_since = None

    def sample(self, driver, now=None):
        now = time.monotonic() if now is None else now
        if self.last_sample is not None and now - self.last_sample < self.sample_seconds:
            return None
        self.last_sample = now
        rss = browser_rss_mb(driver)
        if rss is not None and rss >= self.limit_mb:
            self.high_samples += 1
            if self.high_samples >= 2 and self.pending_since is None:
                self.pending_since = now
        else:
            self.high_samples = 0
            self.pending_since = None
        return rss

    def restart_due(self, cooldown_seconds, now=None):
        now = time.monotonic() if now is None else now
        return self.pending_since is not None and (
            cooldown_seconds >= 45 or now - self.pending_since >= self.max_defer_seconds)
