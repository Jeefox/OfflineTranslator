# -*- coding: utf-8 -*-
"""Метрики benchmark: чистые функции (данные → метрики), без моделей.

Принципы:
- quality-метрики (exact match, chrF) считаются ТОЛЬКО для примеров
  с reference (и только для успешных переводов);
- structural checks НЕ объявляют перевод правильным/неправильным:
  каждая проверка — отдельное структурное свойство (URL/числа/строки
  сохранены, скрипт цели присутствует...); провал проверки означает
  только нарушение конкретного свойства;
- общих "quality score"/"overall score" нет — интерпретация данных
  остаётся за разработчиком.

Только stdlib (math/re/json) — без ML-зависимостей.
"""
import math
import re

# -------------------------------------------------------------------- #
#  Нормализация и text-метрики                                          #
# -------------------------------------------------------------------- #
def normalize_text(text):
    """Схлопывает все последовательности whitespace в один пробел."""
    return " ".join((text or "").split())


def exact_match(hypothesis, reference):
    """Строгое совпадение после нормализации whitespace (case-sensitive).

    Используется только для примеров с flag exact=true (reference
    считается детерминированно-ожидаемым).
    """
    return normalize_text(hypothesis) == normalize_text(reference)


def _char_ngrams(text, n):
    counts = {}
    t = normalize_text(text)
    for i in range(len(t) - n + 1):
        gram = t[i:i + n]
        counts[gram] = counts.get(gram, 0) + 1
    return counts


def chr_f(hypothesis, reference, order=6, beta_sq=2.0):
    """chrF: среднее по порядкам n-грамм (1..order) от F_n character
    n-грамм (β² = 2 — стандартное взвешивание chrF).

    Чистая Python-реализация (без зависимостей). Возвращает
    (F, P, R) — средние по порядкам n-грамм; вход без общих n-грамм —
    0.0.
    """
    fs, ps, rs = [], [], []
    for n in range(1, order + 1):
        hp = _char_ngrams(hypothesis, n)
        rp = _char_ngrams(reference, n)
        overlap = sum(min(hp[g], rp[g]) for g in hp if g in rp)
        total_h = sum(hp.values())
        total_r = sum(rp.values())
        p = overlap / total_h if total_h else 0.0
        r = overlap / total_r if total_r else 0.0
        fs.append((1 + beta_sq) * p * r / (beta_sq * p + r) if (p + r) else 0.0)
        ps.append(p)
        rs.append(r)
    mean = lambda values: sum(values) / len(values) if values else 0.0
    return mean(fs), mean(ps), mean(rs)


# -------------------------------------------------------------------- #
#  Aggregate latency-метрики                                            #
# -------------------------------------------------------------------- #
def p95_nearest_rank(values):
    """p95, nearest-rank: ceil(0.95 * n)-я по возрастанию (1-based).

    При пустом списке — None (честно: метрика недоступна, а не 0).
    """
    if not values:
        return None
    vs = sorted(values)
    idx = max(0, math.ceil(0.95 * len(vs)) - 1)
    return vs[idx]


def median(values):
    """Медиана (при чётном n — среднее двух средних). Пустой список — None."""
    if not values:
        return None
    vs = sorted(values)
    n = len(vs)
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2


def aggregate_latencies(latencies_ms):
    """Сводка по латентности (всех попыток, включая неудачные).

    Пустой список — n=0 и None во всех величинах (не придумываем 0).
    """
    vals = [float(v) for v in latencies_ms]
    if not vals:
        return {"n": 0, "total_ms": None, "avg_ms": None, "median_ms": None,
                "p95_ms": None, "min_ms": None, "max_ms": None}
    return {
        "n": len(vals),
        "total_ms": sum(vals),
        "avg_ms": sum(vals) / len(vals),
        "median_ms": median(vals),
        "p95_ms": p95_nearest_rank(vals),
        "min_ms": min(vals),
        "max_ms": max(vals),
    }


def per_direction_aggregates(samples):
    """{direction: {n, success, failed, total_ms, avg_ms, median_ms,
    p95_ms, min_ms, max_ms}} по всем sample-строкам."""
    out = {}
    for direction in ("en-ru", "ru-en"):
        vals = [s["latency_ms"] for s in samples if s["direction"] == direction]
        agg = aggregate_latencies(vals)
        success = sum(1 for s in samples
                      if s["direction"] == direction and s["success"])
        agg["success"] = success
        agg["failed"] = agg["n"] - success
        out[direction] = agg
    return out



# -------------------------------------------------------------------- #
#  Structural checks (все примеры, независимо от reference)             #
# -------------------------------------------------------------------- #
_URL_RE = re.compile(r"https?://[^\s\"'<>()]+")
_UNIX_PATH_RE = re.compile(r"(?<![\w:/])(?:/(?:[\w.-]+/)+[\w.-]+)")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[\w.-]+\\)*[\w.-]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Dotted code-токены (>= 3 частей: com.example.service.Foo; короткие
# "A.B"/"file.yaml" не считаем, чтобы избежать ложных срабатываний).
_CODE_RE = re.compile(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){2,}\b")
# CLI-флаги вида -DskipTests.
_FLAG_RE = re.compile(r"(?<!\S)-[A-Za-z][\w-]*")
_DIGIT_RE = re.compile(r"\d+")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def extract_urls(source):
    """URL/пути (unix, windows) в source (фиксированный порядок)."""
    found = _URL_RE.findall(source)
    found += _UNIX_PATH_RE.findall(source)
    found += _WIN_PATH_RE.findall(source)
    return sorted(set(found))


def extract_emails(source):
    return sorted(set(_EMAIL_RE.findall(source)))


def extract_code_tokens(source):
    """Code-like токены: dotted-идентификаторы (>=3 частей) и CLI-флаги."""
    found = _CODE_RE.findall(source)
    found += _FLAG_RE.findall(source)
    return sorted(set(found))


def digit_runs(source):
    """Максимальные «забеги» цифр в source (1,000,000 -> ['1','000','000'])."""
    return _DIGIT_RE.findall(source)


def non_empty_lines(text):
    return [line for line in (text or "").splitlines() if line.strip()]


def structural_checks(source, output, direction):
    """Структурные свойства перевода: {check: True/False/None(n/a)}.

    Это НЕ «правильность перевода» и НЕ score: провал, например,
    numbers_preserved означает только, что конкретное число из source
    не найдено в output (числа могли быть переведены словами — это
    зафиксированный структурный факт, а не приговор переводу).
    """
    out = output or ""
    non_empty = bool(out.strip())
    res = {}
    res["output_non_empty"] = non_empty
    if not non_empty:
        res["not_source_copy"] = False
    else:
        res["not_source_copy"] = (
            normalize_text(out) != normalize_text(source))
    if direction == "en-ru":
        res["target_script"] = bool(_CYR_RE.search(out))
    else:
        res["target_script"] = bool(_LATIN_RE.search(out))
    urls = extract_urls(source)
    res["urls_preserved"] = (
        None if not urls else all(u in out for u in urls))
    emails = extract_emails(source)
    res["emails_preserved"] = (
        None if not emails else all(e in out for e in emails))
    runs = digit_runs(source)
    out_digits = re.sub(r"\D", "", out)
    res["numbers_preserved"] = (
        None if not runs else all(run in out_digits for run in runs))
    res["percent_preserved"] = (
        None if "%" not in source else ("%" in out))
    code = extract_code_tokens(source)
    res["code_tokens_preserved"] = (
        None if not code else all(c in out for c in code))
    src_lines = non_empty_lines(source)
    if len(src_lines) >= 2:
        res["lines_preserved"] = len(non_empty_lines(out)) == len(src_lines)
    else:
        res["lines_preserved"] = None
    return res


def structural_summary(samples):
    """Сводка structural checks по всем sample-строкам:
    {check: {"pass": n, "fail": n, "na": n}}."""
    summary = {}
    for s in samples:
        for check, value in s.get("structural", {}).items():
            slot = summary.setdefault(check, {"pass": 0, "fail": 0, "na": 0})
            key = "pass" if value is True else "fail" if value is False else "na"
            slot[key] += 1
    return summary


# -------------------------------------------------------------------- #
#  Quality (только примеры с reference)                                 #
# -------------------------------------------------------------------- #
def compute_quality(samples):
    """Quality-метрики только для успешных примеров с reference.

    Возвращает {direction: {n_ref, chrF_mean, exact_total, exact_matched}}:
    - chrF_mean — по всем reference-примерам (reference — один
      корректный перевод, а не единственный; метрика chrF, не BLEU);
    - exact — ТОЛЬКО по примерам с flag exact=true (строгие
      детерминированные эталоны).
    BLEU намеренно не используется: sentence-BLEU на десятках коротких
    пар с одним reference нестабилен и требует лишней инфраструктуры
    (см. benchmarks/README.md).
    """
    result = {}
    for s in samples:
        if not s.get("reference") or not s.get("success"):
            continue
        bucket = result.setdefault(s["direction"], {
            "n_ref": 0, "chrF_values": [], "exact_total": 0,
            "exact_matched": 0})
        bucket["n_ref"] += 1
        f, _p, _r = chr_f(s["output"], s["reference"])
        bucket["chrF_values"].append(round(f, 4))
        if s.get("exact"):
            bucket["exact_total"] += 1
            if exact_match(s["output"], s["reference"]):
                bucket["exact_matched"] += 1
    for bucket in result.values():
        values = bucket.pop("chrF_values")
        bucket["chrF_mean"] = (
            round(sum(values) / len(values), 4) if values else None)
    return result

