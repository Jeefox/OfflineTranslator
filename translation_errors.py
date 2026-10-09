"""Diagnostic API exceptions and safe messages for the GUI."""


class TranslationError(RuntimeError):
    """Public compatibility base for translation and initialization failures."""


class ModelLoadError(TranslationError):
    """Model initialization failed; original exception is in __cause__."""


class ModelUnavailableError(ModelLoadError):
    """Required local model or runtime dependency is unavailable."""


class BackendError(TranslationError):
    """Tokenization, chunking or native generation failed."""


class ProtectedTokenError(TranslationError):
    """Inference could not preserve protected fragments."""


def to_user_message(error):
    if isinstance(error, ModelUnavailableError):
        return "Модель недоступна. Проверьте локальные файлы и установленный runtime."
    if isinstance(error, ModelLoadError):
        return "Не удалось загрузить модель. Проверьте её файлы или выберите другую модель."
    if isinstance(error, ProtectedTokenError):
        return "Ошибка перевода: модель не сохранила технические фрагменты. Попробуйте другую модель."
    return "Ошибка перевода. Попробуйте снова или выберите другую модель."
