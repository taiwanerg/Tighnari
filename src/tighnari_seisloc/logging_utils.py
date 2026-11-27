"""Keep persistent source failures visible without logging every packet/retry."""

import logging
import threading
import time


class RepeatedLog:
    def __init__(self, logger, interval=300, clock=time.monotonic):
        self.logger, self.interval, self.clock = logger, interval, clock
        self.entries = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self.lock:
            last, skipped = self.entries.get(key, (float("-inf"), 0))
            if now - last < self.interval:
                self.entries[key] = (last, skipped + 1)
                return False, 0
            self.entries[key] = (now, 0)
        return True, skipped

    def warning(self, key, message, *args, exc_info=False):
        allowed, skipped = self.allow(key)
        if not allowed:
            return
        suffix = f"（另略過 {skipped} 次重複訊息）" if skipped else ""
        self.logger.warning(message + suffix, *args, exc_info=exc_info)

    def recovered(self, message, *args):
        with self.lock:
            had_errors = bool(self.entries)
            self.entries.clear()
        if had_errors:
            self.logger.info(message, *args)


class RepeatedFilter(logging.Filter):
    """Apply the same bounded repetition policy to third-party source warnings."""

    def __init__(self):
        super().__init__()
        self.limiter = RepeatedLog(None)

    def filter(self, record):
        allowed, skipped = self.limiter.allow((record.levelno, record.getMessage()))
        if allowed and skipped:
            record.msg = record.getMessage() + f"（另略過 {skipped} 次重複訊息）"
            record.args = ()
        return allowed
