"""Thread-safe LRU cache bounded by entry count and stored characters."""
from collections import OrderedDict
from threading import RLock


class TranslationCache:
    def __init__(self, max_entries=1000, max_chars=2_000_000):
        if max_entries < 0 or max_chars < 0:
            raise ValueError("Cache limits must be non-negative")
        self.max_entries = max_entries
        self.max_chars = max_chars
        self._entries = OrderedDict()
        self._chars = 0
        self._lock = RLock()

    @staticmethod
    def make_key(model_id, direction, source_text):
        return model_id, direction, source_text

    @staticmethod
    def _size(key, value):
        return len(key[-1]) + len(value)

    def get(self, key):
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key, translated_text):
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._chars -= self._size(key, old)
            size = self._size(key, translated_text)
            if not self.max_entries or size > self.max_chars:
                return
            self._entries[key] = translated_text
            self._chars += size
            while len(self._entries) > self.max_entries or self._chars > self.max_chars:
                key, value = self._entries.popitem(last=False)
                self._chars -= self._size(key, value)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._chars = 0

    def __contains__(self, key):
        with self._lock:
            return key in self._entries

    def __len__(self):
        with self._lock:
            return len(self._entries)
