"""Internal serialized radio worker, independent of the apply/readiness path."""
import copy
import threading

from reefy import startup_timing as timing
from reefy.shared import log


class WifiApply:
    def __init__(self, apply):
        self.apply = apply
        self.condition = threading.Condition()
        self.pending = None
        self.running = None
        self.thread = None

    def submit(self, wifi, old_wifi):
        with self.condition:
            target = copy.deepcopy(wifi)
            if self.pending is not None and self.pending[0] == target:
                return
            if self.pending is None and self.running is not None and self.running[0] == target:
                return
            # A newer state must diff against the radio owner, including a
            # connection still in progress, rather than a skipped cloud state.
            previous = self.running[0] if self.running is not None else old_wifi
            self.pending = (target, copy.deepcopy(previous))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True,
                                               name='reefy-wifi-apply')
                self.thread.start()

    def _run(self):
        while True:
            with self.condition:
                if self.pending is None:
                    self.running = None
                    self.thread = None
                    self.condition.notify_all()
                    return
                request = self.running = self.pending
                self.pending = None
            try:
                # This is a separate root: readiness may finish before radio
                # setup. An unfinished span conservatively earns no exclusion.
                with timing.scope('wifi', root=True):
                    self.apply(*request)
            except Exception as exception:
                log('mqtt', f'WiFi worker error ({type(exception).__name__})')
            finally:
                with self.condition:
                    if self.pending is not None:
                        self.pending = (self.pending[0], request[0])
                    self.condition.notify_all()

    def busy(self):
        with self.condition:
            return self.thread is not None

    def wait(self):
        with self.condition:
            while self.thread is not None:
                self.condition.wait()
