"""Оффлайн переводчик EN -> RU на лёгкой предобученной нейросети (Argo Translate).

Модель — локальный файл ``.argosmodel`` (по умолчанию
``translate-en_ru-1_9.argosmodel``). Он ставится один раз через
``package.install_from_path(...)`` и после этого перевод работает
полностью оффлайн, без сети.

Путь к модели настраивается:
  - аргументом ``OfflineTranslator(model_path=...)``;
  - ключом ``model_path`` в config.json (см. ``settings.get_model_path``);
  - при ``None`` — автоопределение (PyInstaller ``_MEIPASS`` / dev-путь)
    и, если модель уже установлена в данных Argos, установка вообще
    пропускается.

Слои:
1. Глоссарий (glossary.txt) — точные переводы слов и фраз (приоритет над NN).
2. Предобученная нейросеть Argo Translate (локальная модель, оффлайн).

Режимы:
- слово / фраза / короткое предложение — глоссарий (если есть), иначе NN;
- длинный технический текст — по абзацам/предложениям.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from argostranslate import package, translate

class _SuppressMwtNotice(logging.Filter):
    """Режет безобидное уведомление Stanza SBD про добавление mwt."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "expects mwt, which has been added" not in record.getMessage()


# Stanza (модуль SBD внутри Argo) при первом переводе выводит в stderr
# "Language en package default expects mwt, which has been added" и при этом
# сбрасывает уровень своего логгера на WARNING — поэтому глушим через Filter,
# который не перебивается сменой уровня.
logging.getLogger("stanza").addFilter(_SuppressMwtNotice())

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_PATH = PROJECT_ROOT / "glossary.txt"

_SENT_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-Z0-9\"'(])")


@dataclass(frozen=True)
class Translation:
    """Результат перевода: текст + источник (нейросеть или глоссарий)."""

    text: str
    source: str  # "nn" | "glossary"


class OfflineTranslator:
    """Полностью оффлайнный переводчик English -> Russian."""

    def __init__(
        self,
        glossary_path: Path = GLOSSARY_PATH,
        model_path: str | Path | None = None,
    ):
        self.glossary_path = Path(glossary_path)
        self._glossary: dict[str, str] = self._load_glossary(self.glossary_path)
        # Путь к .argosmodel: None означает «автоопределение» (см.
        # _resolve_model_path) и пропуск установки, если модель уже есть.
        self._model_path = Path(model_path) if model_path else None
        self._model_ready = False

    # ------------------------------------------------------------------ #
    #  Модель: локальная установка из .argosmodel (без сети)             #
    # ------------------------------------------------------------------ #
    def _resolve_model_path(self) -> Path:
        """Финальный путь к .argosmodel (аргумент -> settings -> авто)."""
        if self._model_path is not None:
            return self._model_path
        # Ленивый импорт: settings не тянет translator, цикла нет.
        from settings import Settings
        return Settings().get_model_path()

    @staticmethod
    def _package_installed() -> bool:
        """Есть ли уже установленная en->ru модель в данных Argos."""
        try:
            installed = package.get_installed_packages()
        except Exception:
            return False
        return any(
            getattr(p, "from_code", None) == "en"
            and getattr(p, "to_code", None) == "ru"
            for p in installed
        )

    def install_model(self) -> None:
        """Ставит модель из локального ``.argosmodel`` (идемпотентно, без сети)."""
        if self._model_ready or self._package_installed():
            self._model_ready = True
            return
        model_file = self._resolve_model_path()
        if not model_file.exists():
            raise FileNotFoundError(
                f"Локальная модель не найдена: {model_file}. "
                "Укажите путь в настройках (model_path) или положите "
                ".argosmodel в ожидаемое место."
            )
        package.install_from_path(str(model_file))
        self._model_ready = True

    def _ensure_ready(self) -> None:
        if not self._model_ready:
            self.install_model()

    def _nn(self, text: str) -> str:
        return translate.translate(text, "en", "ru").strip()

    # ------------------------------------------------------------------ #
    #  Глоссарий (слова и фразы)                                          #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_glossary(path: Path) -> dict[str, str]:
        terms: dict[str, str] = {}
        if not path.exists():
            return terms
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            src, dst = line.split("=", 1)
            terms[src.strip().lower()] = dst.strip()
        return terms

    def add_term(self, source: str, target: str) -> None:
        self._glossary[source.lower()] = target

    def remove_term(self, source: str) -> None:
        self._glossary.pop(source.lower(), None)

    def save_glossary(self) -> None:
        with self.glossary_path.open("w", encoding="utf-8") as f:
            f.write("# Глоссарий: термин=перевод (EN=RU)\n")
            for src, dst in sorted(self._glossary.items()):
                f.write(f"{src}={dst}\n")

    def _glossary_lookup(self, text: str) -> str | None:
        return self._glossary.get(text.strip().lower())

    # ------------------------------------------------------------------ #
    #  Перевод                                                            #
    # ------------------------------------------------------------------ #
    def translate(self, text: str) -> Translation:
        """Слово, фраза или короткое предложение."""
        text = text.strip()
        if not text:
            return Translation("", "nn")
        gl = self._glossary_lookup(text)
        if gl is not None:
            return Translation(gl, "glossary")
        self._ensure_ready()
        return Translation(self._nn(text), "nn")

    def translate_sentence(self, sentence: str) -> Translation:
        """Одно предложение целиком (контекст важен)."""
        sentence = sentence.strip()
        if not sentence:
            return Translation("", "nn")
        self._ensure_ready()
        return Translation(self._nn(sentence), "nn")

    def translate_text(self, text: str) -> str:
        """Длинный текст: абзац за абзацем, предложение за предложением.

        Последовательно, т.к. движок CTranslate2 не потокобезопасен.
        """
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        return "\n\n".join(self._translate_paragraph(p) for p in paragraphs)

    # ------------------------------------------------------------------ #
    #  Инкрементальный перевод (кэш по предложениям)                     #
    # ------------------------------------------------------------------ #
    def _incremental_plan(self, text: str) -> list[list[str]]:
        """Разбивает текст на абзацы -> «единицы перевода».

        Каждая единица — точная строка, которую пошлём в NN (byte-identical
        к тому, что использует ``translate_text``): абзац из одного
        предложения → весь (обрезанный) абзац; иначе — по одному предложению.
        Возвращает список абзацев, каждый — список единиц."""
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        plan: list[list[str]] = []
        for p in paragraphs:
            para = p.strip()
            sentences = self._split_sentences(para)
            if len(sentences) <= 1:
                plan.append([para])
            else:
                plan.append([s for s in sentences if s])
        return plan

    def translate_incremental(
        self, text: str, cache: dict
    ) -> tuple[str, dict]:
        """Инкрементальный перевод с переиспользованием кэша (без стриминга).

        ``cache`` — ``{оригинальная_единица: перевод}``. Для каждого абзаца/
        предложения, уже присутствующего в кэше, NN не вызывается. Возвращает
        ``(полный_перевод, обновлённый_кэш)`` — исходный словарь не меняется,
        создаётся копия с новыми записями.
        """
        new_cache = dict(cache)
        out_paras: list[str] = []
        for units in self._incremental_plan(text):
            if not units:
                continue
            parts: list[str] = []
            for u in units:
                hit = new_cache.get(u)
                if hit is None:
                    hit = self.translate_sentence(u).text
                    new_cache[u] = hit
                parts.append(hit)
            out_paras.append(" ".join(parts))
        return "\n\n".join(out_paras), new_cache

    def translate_incremental_streaming(
        self, text: str, cache: dict, callback
    ) -> tuple[str, dict]:
        """Как ``translate_incremental``, но с промежуточными результатами.

        После каждой переведённой единицы (пропущенной по кэшу тоже) вызывается
        ``callback(translated_so_far, done, total)``. Возвращает
        ``(финальный_перевод, обновлённый_кэш)``.
        """
        new_cache = dict(cache)
        plan = self._incremental_plan(text)
        total = sum(len(u) for u in plan)
        done = 0
        done_paras: list[str] = []

        def _so_far(current_parts: list[str]) -> str:
            chunks = list(done_paras)
            if current_parts:
                chunks.append(" ".join(current_parts))
            return "\n\n".join(chunks)

        for units in plan:
            if not units:
                continue
            parts: list[str] = []
            for u in units:
                hit = new_cache.get(u)
                if hit is None:
                    hit = self.translate_sentence(u).text
                    new_cache[u] = hit
                parts.append(hit)
                done += 1
                if total:
                    callback(_so_far(parts), done, total)
            done_paras.append(" ".join(parts))
        return "\n\n".join(done_paras), new_cache

    def translate_text_streaming(
        self, text: str, callback
    ) -> str:
        """Длинный текст с промежуточными результатами (стриминг).

        Разбивает текст на абзацы/предложения (как ``translate_text``),
        переводит по одному предложению и после каждого вызывает
        ``callback(translated_so_far, done, total)``:

        - ``translated_so_far`` — накопленный перевод в финальном виде
          (абзацы через ``\\n\\n``, предложения внутри абзаца — через
          пробел), но только по завершённым предложениям;
        - ``done`` / ``total`` — сколько предложений готово / всего.

        Возвращает финальный перевод. ``callback`` вызывается в том
        потоке, где вызван метод (например, worker-потоке GUI) — перевод
        UI на главный поток ответственность вызывающего.
        """
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        if not paragraphs:
            return ""
        plan = [(p.strip(), self._split_sentences(p)) for p in paragraphs]
        total = sum(len(sents) for _p, sents in plan)
        done = 0
        done_paras: list[str] = []

        def _so_far(current_parts: list[str]) -> str:
            chunks = list(done_paras)
            if current_parts:
                chunks.append(" ".join(current_parts))
            return "\n\n".join(chunks)

        for para, sents in plan:
            if not sents:
                continue
            if len(sents) == 1:
                done_paras.append(self.translate_sentence(para).text)
                done += 1
                if total:
                    callback(_so_far([]), done, total)
            else:
                parts: list[str] = []
                for s in sents:
                    parts.append(self.translate_sentence(s).text)
                    done += 1
                    if total:
                        callback(_so_far(parts), done, total)
                done_paras.append(" ".join(parts))
        return "\n\n".join(done_paras)

    def _translate_paragraph(self, para: str) -> str:
        para = para.strip()
        if not para:
            return ""
        sentences = self._split_sentences(para)
        if len(sentences) <= 1:
            return self.translate_sentence(para).text
        return " ".join(
            self.translate_sentence(s).text for s in sentences if s
        )

    @staticmethod
    def _split_sentences(para: str) -> list[str]:
        parts = _SENT_RE.split(" " + " ".join(para.split()))
        return [p.strip() for p in parts if p.strip()]
