"""One daemon loader; pending requests coalesce to the most recent one."""
import threading


class ModelLoader:
    def __init__(self):
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, function, *args):
        with self._condition:
            if self._closed:
                return
            self._pending = (function, args)
            self._condition.notify()

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
            function(*args)
