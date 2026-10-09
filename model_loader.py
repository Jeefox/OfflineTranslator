"""One daemon loader; pending requests coalesce to the most recent one."""
import threading
import logging

logger = logging.getLogger(__name__)


class ModelLoader:
    def __init__(self, failure_callback=None):
        self._failure_callback = failure_callback
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, function, *args):
        with self._condition:
            if self._closed:
                return False
            self._pending = (function, args)
            self._condition.notify()
            return True

    def is_alive(self):
        return self._thread.is_alive()

    def close(self):
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                function, args = self._pending
                self._pending = None
            try:
                function(*args)
            except Exception as exc:
                logger.exception("Model loading callback failed")
                if self._failure_callback is not None:
                    try:
                        self._failure_callback(exc, function, args)
                    except Exception:
                        logger.exception("Model loader failure callback failed")
