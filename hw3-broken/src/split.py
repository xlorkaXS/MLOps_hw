"""Стадия split: разбиение на train/val/test."""

import json
import random
import time
from pathlib import Path

from src.config import load_params
from src.contamination import is_clean, report
from src.schema import Example, dump, iter_examples
from src.textnorm import normalize_group


def group_split(examples: list[Example], ratios: dict[str, float], seed: int) -> dict[str, list[Example]]:
    """Разложить целые темы по сплитам, сохраняя приблизительные доли строк."""
    if set(ratios) != {"train", "val", "test"} or abs(sum(ratios.values()) - 1) > 1e-9:
        raise ValueError("split.ratios должны задавать train/val/test с суммой 1")
    groups: dict[str, list[Example]] = {}
    for ex in examples:
        groups.setdefault(normalize_group(ex.topic), []).append(ex)
    if len(groups) < len(ratios):
        raise ValueError("для разбиения нужны как минимум три разные темы")

    names = list(ratios)
    keys = list(groups)
    random.Random(seed).shuffle(keys)
    keys.sort(key=lambda key: len(groups[key]), reverse=True)
    buckets: dict[str, list[Example]] = {name: [] for name in names}
    targets = {name: len(examples) * ratios[name] for name in names}
    for key in keys:
        name = max(names, key=lambda part: targets[part] - len(buckets[part]))
        buckets[name].extend(groups[key])
    return buckets


def main() -> None:
    params = load_params()
    paths = params["paths"]
    cfg = params["split"]
    started = time.perf_counter()

    examples: list[Example] = list(iter_examples(paths["clean"]))
    if cfg["group_key"] != "topic":
        raise SystemExit(f"неизвестный split.group_key: {cfg['group_key']!r}")

    sizes: dict[str, int] = {}
    for ex in examples:
        key = normalize_group(ex.topic)
        sizes[key] = sizes.get(key, 0) + 1

    buckets = group_split(examples, cfg["ratios"], cfg["seed"])

    for name, rows in buckets.items():
        out = Path(paths[name])
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for ex in rows:
                fh.write(dump(ex) + "\n")

    nd = params["clean"]["near_dup"]
    if not nd["enabled"] or nd["threshold"] != params["contamination"]["threshold"]:
        raise SystemExit("пороги near-dup очистки и проверки контаминации должны совпадать")
    rep = report(
        buckets["train"],
        buckets["test"],
        shingle_words=nd["shingle_words"],
        num_perm=nd["num_perm"],
        threshold=params["contamination"]["threshold"],
    )

    metrics = {
        "version": params["collect"]["version"],
        "seed": cfg["seed"],
        "group_key": cfg["group_key"],
        "groups_total": len(sizes),
        "sizes": {name: len(rows) for name, rows in buckets.items()},
        "groups": {
            name: len({normalize_group(ex.topic) for ex in rows}) for name, rows in buckets.items()
        },
        "ratios_actual": {
            name: round(len(rows) / len(examples), 4) for name, rows in buckets.items()
        },
        "contamination": rep,
        "seconds": round(time.perf_counter() - started, 2),
    }
    mpath = Path(paths["metrics_split"])
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not is_clean(rep):
        raise SystemExit(f"split: обнаружена контаминация train/test: {rep}")

    print(
        "split: "
        + ", ".join(f"{name} {len(rows)}" for name, rows in buckets.items())
        + f" (групп {len(sizes)}, {metrics['seconds']} с)"
    )


if __name__ == "__main__":
    main()
