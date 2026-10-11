# Безопасность зависимостей и моделей

Runtime использует закреплённые версии из `requirements.txt`, release — из
`requirements-release.txt`. Инструменты установки/сборки закреплены отдельно
в `requirements-build-tools.txt`; установите их до остальных зависимостей.
Минимальный PyTorch для конвертера — 2.13.0: эта версия включает исправления
[GHSA-63cw-57p8-fm3p](https://github.com/advisories/GHSA-63cw-57p8-fm3p)
и [GHSA-rrmf-rvhw-rf47](https://github.com/advisories/GHSA-rrmf-rvhw-rf47).

## Проверка установленного графа

После установки runtime/release зависимостей в чистом virtualenv:

```bash
python -m pip install -r requirements-audit.txt
python -m pip_audit --vulnerability-service osv
```

CI проверяет установленный граф вместе с транзитивными зависимостями.
Новые advisory приводят к ошибке проверки; pinned версия сама по себе
не означает отсутствие уязвимостей. Проверка PyPI-пакетов не заменяет
проверку встроенных native библиотек и готовых release-бинарников.

Каждая release-сборка сохраняет CycloneDX SBOM установленного Python-окружения,
снимок всех версий (`.resolved.txt`) и сведения о Python/ОС/архитектуре.
Инвентаризация включает build tooling, поэтому не равна содержимому бинарника.
Снимок помогает восстановить конкретный граф с исходными package indexes;
полный входной lock с хешами и отдельными вариантами для ОС пока не создан.

## Известное исключение дополнительного GGUF runtime

`llama-cpp-python==0.3.36` требует `diskcache`, текущая версия 5.6.3 имеет
[GHSA-w8v5-vhqr-4h9v / CVE-2025-69872](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v).
На 11 октября 2026 исправленной версии не опубликовано. Уязвимость позволяет
выполнить код при чтении pickle из кэша, в который атакующий может писать.

OfflineTranslator не создаёт `LlamaDiskCache`, не вызывает `set_cache` и
использует собственный bounded in-memory `TranslationCache` без pickle.
Стандартный Marian-бандль исключает `llama_cpp`. Достижимость этой уязвимости
в текущем GGUF-пути не подтверждена; сама зависимость остаётся уязвимой.
Не подключайте чужие дисковые кэши; включение дискового кэша требует нового
аудита. Исключение нужно пересмотреть при обновлении llama-cpp-python/diskcache.

Только для окружения с GGUF разрешено точечное исключение:

```bash
python -m pip_audit --vulnerability-service osv --ignore-vuln GHSA-w8v5-vhqr-4h9v
```

Это исключение используется в optional GGUF CI job; обычная release-проверка
не имеет исключений. Другие CVE, включая новые advisory diskcache, не подавляются.

## Модели

Marian runtime принимает safetensors, с `trust_remote_code=False`.
Конвертер pickle-весов — отдельный шаг для фиксированных официальных ревизий;
он не принимает произвольные репозитории. GGUF/tokenizer файлы разбираются
native кодом в процессе приложения: загружайте модели из доверенных источников.
