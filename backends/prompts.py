# -*- coding: utf-8 -*-
"""Model-specific prompt для POC-модели GGUF (Tencent Hy-MT2).

POC фиксирует ОДНУ конкретную translation-oriented модель:

    tencent/Hy-MT2-1.8B  (GGUF: tencent/Hy-MT2-1.8B-GGUF)

- специализированная мультиязычная translation-модель (33 языка,
  включая en и ru); архитектура HunYuanDenseV1 (в llama.cpp —
  «hunyuan-dense»); контекст модели 256K;
- официальный GGUF издателя, inference через llama.cpp (CPU).

Prompt-формат (всё взято из официальных артефактов модели):

1) Chat template — файл chat_template.jinja из репозитория
   tencent/Hy-MT2-1.8B. У модели НЕТ system prompt
   («our models do not have a default system_prompt»), поэтому
   для одного user-сообщения template даёт ровно:

       <｜hy_begin▁of▁sentence｜><｜hy_User｜>{user content}<｜hy_Assistant｜>

   (в GGUF chat template не встроен, поэтому prompt собирается
   явно — детерминированно и не зависит от runtime).

2) Инструкция перевода — официальный шаблон «Default Translation»
   (English prompt) из model card. Направление задаётся ЯВНО полным
   названием целевого языка (не определяется по содержимому текста):

       Translate the following text into {target_lang}. Note that you
       should only output the translated result without any additional
       explanation:\n\n{source_text}

3) Sampling — рекомендации model card для 1.8B/7B (совпадает с
   generation_config.json, кроме top_p: card — 0.6, config — 0.8;
   взято значение card): temperature 0.7, top_p 0.6, top_k 20,
   repetition_penalty 1.05.

   ИСКЛЮЧЕНИЕ ДЛЯ POC: temperature = 0.0 (greedy). Причина: критерий
   Этапа 6 требует, чтобы final output translate_stream() совпадал с
   translate() по одному и тому же тексту; при temperature > 0
   генерация неселективна по случайному состоянию runtime и повторные
   вызовы дают разные варианты (проверено на реальной модели).
   Greedy (T=0) детерминирован. Рекомендованное card значение 0.7
   легко вернуть, поменяв SAMPLING ниже (top_p/top_k/repeat_penalty
   от card сохранены и не влияют на детерминизм при T=0).

4) EOS — <｜hy_place▁holder▁no▁2｜> (id 120020, special_tokens_map.json).

Это минимальный адаптер под одну модель (не система prompt-шаблонов):
смена модели POC = замена констант и build_* функций в этом файле.
"""

#: Специальные токены Hy-MT2 (tokenizer: added_tokens_decoder, id 120000+).
BOS = "<｜hy_begin▁of▁sentence｜>"      # id 120000
USER = "<｜hy_User｜>"                  # id 120006
ASSISTANT = "<｜hy_Assistant｜>"        # id 120007
EOS = "<｜hy_place▁holder▁no▁2｜>"      # id 120020 (eos_token)

#: Sampling-параметры Hy-MT2 1.8B/7B (model card; имена аргументов
#: llama-cpp-python: temperature/top_p/top_k/repeat_penalty).
#: POC: temperature=0.0 (greedy) — детерминированные результаты
#: (критерий: translate_stream() == translate()); card рекомендует
#: temperature=0.7 — вернуть одной правкой здесь.
SAMPLING = {
    "temperature": 0.0,  # POC: greedy (card: 0.7)
    "top_p": 0.6,
    "top_k": 20,
    "repeat_penalty": 1.05,
}

#: direction -> целевой язык ПОЛНЫМ названием (явное направление;
#: model card: «target_lang should use the full language names»).
_TARGET_LANG = {
    "en-ru": "Russian",
    "ru-en": "English",
}


def build_translation_instruction(text: str, direction: str) -> str:
    """Инструкция перевода (содержимое user-сообщения) по официальному
    шаблону «Default Translation» (English prompt) Hy-MT2.

    direction — 'en-ru' или 'ru-en'; неизвестное — ValueError.
    """
    try:
        target = _TARGET_LANG[direction]
    except KeyError:
        raise ValueError(
            "Hy-MT2 prompt: неподдерживаемое направление %r "
            "(ожидается: en-ru, ru-en)" % (direction,)
        ) from None
    return (
        "Translate the following text into %s. Note that you should "
        "only output the translated result without any additional "
        "explanation:\n\n%s" % (target, text)
    )


def build_raw_prompt(text: str, direction: str) -> str:
    """Полный prompt для inference: официальный chat template
    (без system) + инструкция перевода.

    Токенизуется одним вызовом с special=True, чтобы специальные
    токены Hy-MT2 стали единичными токенами (как в transformers
    apply_chat_template).
    """
    return "%s%s%s%s" % (
        BOS, USER, build_translation_instruction(text, direction), ASSISTANT)
