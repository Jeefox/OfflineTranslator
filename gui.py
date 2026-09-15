"""Графический интерфейс оффлайн-переводчика EN -> RU (tkinter).

Макет (grid):
  0-я строка  — LabelFrame «Английский текст» (вход)
  1-я строка  — LabelFrame «Перевод (Русский)» (выход, только чтение)
  2-я строка  — кнопка «Очистить» и статус-бар справа

Перевод выполняется автоматически: 1.5 секунды после последнего нажатия
клавиши во входном поле (debounce через root.after / root.after_cancel).

Текстовые поля занимают всё свободное место (grid weight),
кнопка и статус прижаты к нижнему краю.

Логика перевода вынесена в:func:`translate_text` — её легко заменить
своим вызовом лёгкой оффлайн-нейросети.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from offline_translate import OfflineTranslator

_TRANSLATOR = OfflineTranslator()  # одна инстанция на всё приложение

DEBOUNCE_MS = 1500  # пауза после последнего ввода, после которой переводится


def translate_text(source: str) -> str:
    """Точка входа для перевода: английская строка -> русская.

    Замените тело на вызов своей нейросети, если нужно.
    """
    return _TRANSLATOR.translate_text(source)


class TranslatorGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._timer_id: str | None = None     # id отложенного перевода (debounce)
        self._pending = False                 # идёт ли сейчас перевод в потоке
        self._request_id = 0                  # номер текущего запроса;
                                              # устаревшие результаты игнорируем

        root.title("Оффлайн-переводчик EN -> RU")
        root.geometry("800x520")
        root.minsize(520, 360)

        self._build()
        self.in_text.bind("<<Modified>>", self._on_text_modified)
        root.after(100, self._poll_results)

    # ------------------------------------------------------------------ #
    #  Верстка                                                           #
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=1)   # вход — растягивается
        root.grid_rowconfigure(1, weight=1)   # выход — растягивается
        root.grid_rowconfigure(2, weight=0)   # кнопка + статус

        # --- Строка 0: входной текст -----------------------------------
        top = tk.LabelFrame(root, text=" Английский текст ",
                            font=("Segoe UI", 10, "bold"))
        top.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(0, weight=1)
        self.in_text = tk.Text(top, wrap="word", undo=True,
                               font=("Consolas", 11), bd=0)
        in_scroll = ttk.Scrollbar(top, command=self.in_text.yview)
        self.in_text.configure(yscrollcommand=in_scroll.set)
        self.in_text.grid(row=0, column=0, sticky="nsew")
        in_scroll.grid(row=0, column=1, sticky="ns")

        # --- Строка 1: выходной текст (только чтение) -------------------
        bottom = tk.LabelFrame(root, text=" Перевод (Русский) ",
                               font=("Segoe UI", 10, "bold"))
        bottom.grid(row=1, column=0, sticky="nsew", padx=8, pady=(4, 4))
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_rowconfigure(0, weight=1)
        self.out_text = tk.Text(bottom, wrap="word", state="disabled",
                                font=("Consolas", 11), bd=0,
                                bg="#fbfbfb")
        out_scroll = ttk.Scrollbar(bottom, command=self.out_text.yview)
        self.out_text.configure(yscrollcommand=out_scroll.set)
        self.out_text.grid(row=0, column=0, sticky="nsew")
        out_scroll.grid(row=0, column=1, sticky="ns")

        # --- Строка 2: кнопка + статус ----------------------------------
        bar = tk.Frame(root)
        bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        bar.grid_columnconfigure(1, weight=1)   # статус прижат к правому краю
        self.btn_clear = tk.Button(bar, text="Очистить",
                                   command=self._on_clear, padx=14, pady=6)
        self.btn_clear.grid(row=0, column=0, sticky="w")
        self.status = tk.Label(bar, text="Готово", fg="#5a5a5a",
                               font=("Segoe UI", 9))
        self.status.grid(row=0, column=2, sticky="e")

    # ------------------------------------------------------------------ #
    #  Обработчики                                                       #
    # ------------------------------------------------------------------ #
    def _set_output(self, text: str) -> None:
        self.out_text.configure(state="normal")
        self.out_text.delete("1.0", tk.END)
        self.out_text.insert("1.0", text)
        self.out_text.configure(state="disabled")

    def _set_status(self, text: str, color: str = "#5a5a5a") -> None:
        self.status.configure(text=text, fg=color)

    def _on_text_modified(self, _event=None) -> None:
        """Дебаунс: любое изменение текста сбрасывает и запускает таймер
        на DEBOUNCE_MS. Пока таймер не сработал, новые нажатия клавиш
        отменяют предыдущий через root.after_cancel()."""
        # <<Modified>> срабатывает многократно на одно изменение —
        # снимаем флаг, иначе событие будет обрабатываться снова.
        self.in_text.edit_modified(False)

        # Отменяем предыдущий отложенный запуск, если он ещё не сработал
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
        self._timer_id = self.root.after(DEBOUNCE_MS, self._start_translate)

    def _start_translate(self) -> None:
        """Срабатывает через 1.5 с после последнего ввода."""
        self._timer_id = None
        source = self.in_text.get("1.0", tk.END).strip()
        if not source:
            self._set_output("")
            self._set_status("Готово")
            return
        if self._pending:  # предыдущий перевод ещё идёт — ждём его
            return
        self._pending = True
        self._request_id += 1
        req_id = self._request_id
        self._set_status("Перевод…", "#0a7d34")
        threading.Thread(target=self._worker, args=(source, req_id),
                         daemon=True).start()

    def _on_clear(self) -> None:
        # Отменяем отложенный автоперевод и делаем недействительными
        # результаты уже запущенных потоков
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
            self._timer_id = None
        self._request_id += 1
        self._pending = False
        self.in_text.edit_modified(False)
        self.in_text.delete("1.0", tk.END)
        self._set_output("")
        self._set_status("Готово")

    # ------------------------------------------------------------------ #
    #  Фоновый перевод + возврат результата в Tk                        #
    # ------------------------------------------------------------------ #
    def _worker(self, source: str, req_id: int) -> None:
        """Выполняется в отдельном потоке, чтобы не блокировать UI."""
        try:
            self._queue.put(("ok", req_id, translate_text(source)))
        except Exception:  # noqa: BLE001
            import traceback
            self._queue.put(("error", req_id, traceback.format_exc()))

    def _poll_results(self) -> None:
        try:
            while True:
                kind, req_id, payload = self._queue.get_nowait()
                if req_id != self._request_id:
                    # Текст уже изменился — устаревший результат отбрасываем
                    continue
                if kind == "ok":
                    self._set_output(payload)
                    self._set_status("Готово", "#0a7d34")
                else:
                    self._set_output("Ошибка перевода:\n" + payload)
                    self._set_status("Ошибка", "#c0392b")
                # Результат получен — снимаем режим «Перевод…»
                self._pending = False
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)


def main() -> None:
    root = tk.Tk()
    TranslatorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
