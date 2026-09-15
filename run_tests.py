"""Быстрый самопроверочный сценарий: слова, фразы, предложения, тех. текст."""
from __future__ import annotations

import io

from offline_translate import OfflineTranslator


def main() -> None:
    t = OfflineTranslator()
    out = io.open("test_results.txt", "w", encoding="utf-8")

    out.write("=== WORDS / PHRASES / SENTENCES ===\n")
    short_cases = [
        "machine learning",
        "neural network",
        "garbage collector",
        "the system allocates memory for the buffer",
        "What is the latency of the request?",
    ]
    for s in short_cases:
        out.write(f"{s!r:55} -> {t.translate(s).text}\n")

    out.write("\n=== TECHNICAL TEXT (chunk) ===\n")
    tech = (
        "The kernel allocates virtual memory pages for each process.\n"
        "If a page fault occurs, the scheduler preempts the current task "
        "and dispatches the interrupt handler.\n\n"
        "Latency spikes may indicate a memory leak or an exhausted "
        "connection pool. Verify the garbage collector settings and "
        "increase the heap size if necessary."
    )
    out.write(t.translate_text(tech))
    out.close()


if __name__ == "__main__":
    main()
