"""Переводчик на базе CTranslate2 + SentencePiece.

Argo Translate / Stanza / Torch не используются: перевод идёт напрямую через
CTranslate2, токенизация — SentencePiece. Модель — ``.argosmodel`` (zip с
``model/model.bin`` + ``sentencepiece.model``) либо распакованная папка.

Публичный интерфейс приложения — ``OfflineTranslator`` (глоссарий,
``Translation``, стриминг) поверх низкоуровневого ``Translator``.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import ctranslate2
import sentencepiece as spm


def _split_sentences(text: str, max_len: int = 500) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = re.split(r'(?<=[.!?])\s+|\n+', text)
    chunks, current = [], ""
    for part in parts:
        if len(current) + len(part) + 1 > max_len:
            if current:
                chunks.append(current.strip())
            current = part
        else:
            current = (current + " " + part).strip() if current else part
    if current:
        chunks.append(current.strip())
    return chunks if chunks else [text]


class Translator:
    def __init__(self, model_path: str | Path):
        self._model_path = Path(model_path)
        self._tmp_dir = None
        self._model_dir = None
        self._src_sp = None
        self._tgt_sp = None
        self._translator = None
        self._load()

    def _load(self):
        if self._model_path.suffix == '.argosmodel':
            self._tmp_dir = tempfile.mkdtemp(prefix='offline_translate_')
            with zipfile.ZipFile(self._model_path, 'r') as z:
                z.extractall(self._tmp_dir)
            self._model_dir = self._tmp_dir
        else:
            self._model_dir = str(self._model_path)

        sp_path = os.path.join(self._model_dir, 'sentencepiece.model')
        if not os.path.exists(sp_path):
            for root, _, files in os.walk(self._model_dir):
                if 'sentencepiece.model' in files:
                    sp_path = os.path.join(root, 'sentencepiece.model')
                    break
        if not os.path.exists(sp_path):
            raise FileNotFoundError(f"sentencepiece.model не найден в {self._model_dir}")

        # Конструктор SentencePieceProcessor не принимает путь: модель
        # загружается через .load().
        self._src_sp = spm.SentencePieceProcessor()
        self._src_sp.load(sp_path)
        self._tgt_sp = spm.SentencePieceProcessor()
        self._tgt_sp.load(sp_path)

        ct_model_path = os.path.join(self._model_dir, 'model')
        if not os.path.isdir(ct_model_path):
            for root, _, files in os.walk(self._model_dir):
                if 'model.bin' in files:
                    ct_model_path = root
                    break
        if not os.path.isdir(ct_model_path):
            raise FileNotFoundError(f"CTranslate2 модель не найдена в {self._model_dir}")

        self._translator = ctranslate2.Translator(
            ct_model_path, device='cpu',
            inter_threads=1, intra_threads=4, compute_type='default'
        )

    def translate(self, text: str) -> str:
        if not text.strip():
            return ""
        sentences = _split_sentences(text, max_len=1000)
        parts = []
        for sentence in sentences:
            tokens = self._src_sp.encode(sentence, out_type=str)
            results = self._translator.translate_batch([tokens])
            translated_tokens = results[0].hypotheses[0]
            parts.append(self._tgt_sp.decode(translated_tokens))
        return ' '.join(parts)

    def close(self) -> None:
        if self._tmp_dir and os.path.exists(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------- #
#  Совместимый слой приложения (глоссарий + стриминг)                    #
# ---------------------------------------------------------------------- #
PROJECT_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_PATH = PROJECT_ROOT / "glossary.txt"

_SENT_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-Z0-9\"'(])")


@dataclass(frozen=True)
class Translation:
    """Результат перевода: текст + источник (нейросеть или глоссарий)."""

    text: str
    source: str  # "nn" | "glossary"


class OfflineTranslator:
    """Оффлайнный переводчик EN -> RU: глоссарий + CTranslate2-нейросеть."""

    def __init__(
        self,
        glossary_path: Path = GLOSSARY_PATH,
        model_path: str | Path | None = None,
    ):
        self.glossary_path = Path(glossary_path)
        self._glossary: dict[str, str] = self._load_glossary(self.glossary_path)
        self._model_path = Path(model_path) if model_path else None
        self._engine: Translator | None = None

    # ------------------------------------------------------------------ #
    #  Модель (ленивая загрузка)                                         #
    # ------------------------------------------------------------------ #
    def _resolve_model_path(self) -> Path:
        if self._model_path is not None:
            return self._model_path
        # Ленивый импорт: settings не тянет translator, цикла нет.
        from settings import Settings
        return Settings().get_model_path()

    def install_model(self) -> None:
        if self._engine is None:
            model_file = self._resolve_model_path()
            if not model_file.exists():
                raise FileNotFoundError(
                    f"Локальная модель не найдена: {model_file}. "
                    "Укажите путь в настройках (model_path) или положите "
                    ".argosmodel в ожидаемое место."
                )
            self._engine = Translator(model_file)

    def _ensure_ready(self) -> None:
        if self._engine is None:
            self.install_model()

    def _nn(self, text: str) -> str:
        self._ensure_ready()
        assert self._engine is not None
        return self._engine.translate(text)

    # ------------------------------------------------------------------ #
    #  Глоссарий (слова и фразы)                                         #
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
    #  Перевод                                                           #
    # ------------------------------------------------------------------ #
    def translate(self, text: str) -> Translation:
        """Слово, фраза или короткое предложение."""
        text = text.strip()
        if not text:
            return Translation("", "nn")
        gl = self._glossary_lookup(text)
        if gl is not None:
            return Translation(gl, "glossary")
        return Translation(self._nn(text), "nn")

    def translate_sentence(self, sentence: str) -> Translation:
        """Одно предложение целиком (контекст важен)."""
        sentence = sentence.strip()
        if not sentence:
            return Translation("", "nn")
        return Translation(self._nn(sentence), "nn")

    def translate_text(self, text: str) -> str:
        """Длинный текст: абзац за абзацем, предложение за предложением.

        Последовательно, т.к. движок CTranslate2 не потокобезопасен."""
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        return "\n\n".join(self._translate_paragraph(p) for p in paragraphs)

    # ------------------------------------------------------------------ #
    #  Инкрементальный перевод (кэш по предложениям)                     #
    # ------------------------------------------------------------------ #
    def _incremental_plan(self, text: str) -> list[list[str]]:
        """Разбивает текст на абзацы -> «единицы перевода».

        Каждая единица — точная строка, которую пошлём в NN: абзац из одного
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
        ``callback(translated_so_far, done, total)``.

        Возвращает финальный перевод. ``callback`` вызывается в том
        потоке, где вызван метод (например, worker-потоке GUI).
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

    def __del__(self):
        try:
            if self._engine is not None:
                self._engine.close()
        except Exception:  # noqa: BLE001
            pass
