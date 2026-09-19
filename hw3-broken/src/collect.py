"""Преобразовать архивный CSV экзопланет в русскоязычный chat-датасет.

Одна запись PSCompPars даёт не более одного примера. Тип вопроса и вариант
системной инструкции определяются хэшем названия планеты: v1 является строгим
подмножеством v2, а повторный запуск на том же снимке источника детерминирован.
"""

import csv
import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path

from src.config import load_params

FIELDS = {
    "pl_name", "hostname", "disc_year", "discoverymethod", "pl_orbper",
    "pl_rade", "pl_bmasse", "sy_dist", "st_teff",
}
QUESTION_TYPES = ("discovery", "period", "radius", "mass", "distance", "temperature")


def stable_choice(value: str, size: int) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % size


def positive_number(raw: str | None) -> float | None:
    try:
        value = float(raw or "")
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def number(raw: str | None) -> str | None:
    value = positive_number(raw)
    return format(value, ".4g") if value is not None else None


def qa(row: dict[str, str], kind: str) -> tuple[str, str] | None:
    name = row["pl_name"].strip()
    host = row["hostname"].strip()
    if kind == "discovery":
        year = row["disc_year"].strip()
        method = row["discoverymethod"].strip()
        if not year.isdigit() or not (1900 <= int(year) <= 2100) or not method:
            return None
        return (
            f"В каком году и каким методом обнаружена экзопланета {name}?",
            f"Согласно NASA Exoplanet Archive, {name} открыта в {year} году; метод обнаружения — {method}.",
        )
    if kind == "period" and (value := number(row["pl_orbper"])):
        return (f"Каков орбитальный период экзопланеты {name}?",
                f"Орбитальный период {name} составляет {value} суток.")
    if kind == "radius" and (value := number(row["pl_rade"])):
        return (f"Каков радиус экзопланеты {name} в радиусах Земли?",
                f"Радиус {name} составляет {value} радиусов Земли.")
    if kind == "mass" and (value := number(row["pl_bmasse"])):
        return (f"Какова оценка массы экзопланеты {name} в массах Земли?",
                f"Оценка массы {name} — {value} масс Земли.")
    if kind == "distance" and (value := number(row["sy_dist"])):
        return (f"На каком расстоянии от Солнца находится система экзопланеты {name}?",
                f"Система звезды {host}, к которой относится {name}, находится примерно в {value} парсеках от Солнца.")
    if kind == "temperature" and (value := number(row["st_teff"])):
        return (f"Какова эффективная температура звезды экзопланеты {name}?",
                f"Эффективная температура звезды {host}, вокруг которой обращается {name}, составляет {value} К.")
    return None


def main() -> None:
    params = load_params()
    cfg = params["collect"]
    version = cfg["version"]
    targets = cfg["rows_by_version"]
    if version not in targets or targets[version] <= 0:
        raise SystemExit(f"неизвестная или пустая версия collect.version: {version!r}")
    variants = cfg["system_prompts"]
    if len(variants) < 3 or any(not text.strip() for text in variants):
        raise SystemExit("collect.system_prompts: нужны не менее трёх непустых вариантов")
    source = Path(cfg["source"])
    if not source.is_file():
        raise SystemExit(f"нет снимка источника {source}; восстановите его через DVC или make source")

    out = Path(params["paths"]["raw"])
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    scanned = skipped_identity = skipped_no_fact = skipped_duplicate = 0
    seen_names: set[str] = set()
    candidates: list[tuple[str, str, int, dict]] = []
    type_counts: Counter[str] = Counter()
    prompt_counts: Counter[int] = Counter()

    with source.open(newline="", encoding="utf-8") as src:
        reader = csv.DictReader(src)
        if not FIELDS.issubset(reader.fieldnames or []):
            raise SystemExit(f"CSV источника не содержит обязательные поля: {sorted(FIELDS - set(reader.fieldnames or []))}")
        for row in reader:
            scanned += 1
            name = (row["pl_name"] or "").strip()
            host = (row["hostname"] or "").strip()
            if not name or not host:
                skipped_identity += 1
                continue
            if name in seen_names:
                skipped_duplicate += 1
                continue
            seen_names.add(name)
            first = stable_choice(name, len(QUESTION_TYPES))
            selected = next(
                ((kind, pair) for kind in (QUESTION_TYPES[(first + i) % len(QUESTION_TYPES)]
                                      for i in range(len(QUESTION_TYPES)))
                 if (pair := qa(row, kind)) is not None),
                None,
            )
            if selected is None:
                skipped_no_fact += 1
                continue
            kind, (question, answer) = selected
            prompt_index = stable_choice(name + ":system", len(variants))
            record = {
                "id": "exo-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:20],
                "topic": f"Звёздная система {host}",
                "messages": [
                    {"role": "system", "content": variants[prompt_index]},
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": answer},
                ],
            }
            order_key = hashlib.sha256(name.encode("utf-8")).hexdigest()
            candidates.append((order_key, kind, prompt_index, record))

    candidates.sort(key=lambda item: item[0])
    selected_rows = candidates[:targets[version]]
    written = len(selected_rows)
    if written < targets[version]:
        raise SystemExit(f"источник дал только {written} пригодных строк из {targets[version]}")
    with out.open("w", encoding="utf-8") as dst:
        for _, kind, prompt_index, record in selected_rows:
            dst.write(json.dumps(record, ensure_ascii=False) + "\n")
            type_counts[kind] += 1
            prompt_counts[prompt_index] += 1
    metrics = {
        "version": version,
        "source": str(source),
        "rows_scanned": scanned,
        "rows_written": written,
        "skipped_missing_identity": skipped_identity,
        "skipped_duplicate_planet": skipped_duplicate,
        "skipped_no_valid_fact": skipped_no_fact,
        "question_types": dict(sorted(type_counts.items())),
        "system_prompt_variants": len(prompt_counts),
        "seconds": round(time.perf_counter() - started, 2),
    }
    mpath = Path(params["paths"]["metrics_collect"])
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"collect: {version}, просмотрено {scanned}, записано {written}, типов вопросов {len(type_counts)}")


if __name__ == "__main__":
    main()
