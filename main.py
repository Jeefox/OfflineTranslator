# -*- coding: utf-8 -*-

import customtkinter as ctk
from tkinter import messagebox
from translator import OfflineTranslator
import threading

# Настройка внешнего вида
ctk.set_appearance_mode("Dark")  # Темная тема (или "Light", "System")
ctk.set_default_color_theme("blue")

class TranslatorApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Офлайн Переводчик (EN -> RU)")
        self.geometry("900x600")
        self.resizable(True, True)

        # Инициализация переводчика в отдельном потоке, чтобы окно не зависло при загрузке
        self.translator = None
        self.status_label = ctk.CTkLabel(self, text="Инициализация нейросети...", text_color="orange")
        self.status_label.pack(pady=10)

        # Запускаем загрузку модели в фоне
        threading.Thread(target=self.init_translator, daemon=True).start()

        self.setup_ui()

    def init_translator(self):
        try:
            self.translator = OfflineTranslator()
            self.status_label.configure(text="Готов к переводу! (Работает офлайн)", text_color="green")
            self.translate_btn.configure(state="normal")
        except Exception as e:
            self.status_label.configure(text=f"Ошибка загрузки: {e}", text_color="red")

    def setup_ui(self):
        # Главный контейнер
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # Поле ввода (Английский)
        self.input_label = ctk.CTkLabel(main_frame, text="Английский текст (EN):")
        self.input_label.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        
        self.input_text = ctk.CTkTextbox(main_frame, width=400, height=400, font=("Arial", 14))
        self.input_text.grid(row=1, column=0, padx=10, pady=10)

        # Поле вывода (Русский)
        self.output_label = ctk.CTkLabel(main_frame, text="Русский перевод (RU):")
        self.output_label.grid(row=0, column=1, padx=10, pady=10, sticky="w")
        
        self.output_text = ctk.CTkTextbox(main_frame, width=400, height=400, font=("Arial", 14))
        self.output_text.grid(row=1, column=1, padx=10, pady=10)

        # Кнопки управления
        btn_frame = ctk.CTkFrame(main_frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=20)

        self.translate_btn = ctk.CTkButton(btn_frame, text="Перевести", command=self.start_translation, state="disabled", width=150, height=40, font=("Arial", 16, "bold"))
        self.translate_btn.pack(side="left", padx=10)

        copy_btn = ctk.CTkButton(btn_frame, text="Копировать перевод", command=self.copy_translation, width=150, height=40)
        copy_btn.pack(side="left", padx=10)

        clear_btn = ctk.CTkButton(btn_frame, text="Очистить", command=self.clear_fields, width=100, height=40, fg_color="gray")
        clear_btn.pack(side="left", padx=10)

    def start_translation(self):
        text = self.input_text.get("1.0", "end-1c")
        if not text.strip():
            return
        
        self.status_label.configure(text="Перевод...", text_color="yellow")
        self.translate_btn.configure(state="disabled")
        
        # Перевод в отдельном потоке, чтобы GUI не фризил
        threading.Thread(target=self.translate_thread, args=(text,), daemon=True).start()

    def translate_thread(self, text):
        result = self.translator.translate(text)
        # Возвращаемся в главный поток для обновления GUI
        self.after(0, self.update_output, result)

    def update_output(self, result):
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", result)
        self.status_label.configure(text="Готов к переводу! (Работает офлайн)", text_color="green")
        self.translate_btn.configure(state="normal")

    def copy_translation(self):
        text = self.output_text.get("1.0", "end-1c")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status_label.configure(text="Скопировано в буфер обмена!", text_color="green")

    def clear_fields(self):
        self.input_text.delete("1.0", "end")
        self.output_text.delete("1.0", "end")

if __name__ == "__main__":
    app = TranslatorApp()
    app.mainloop()