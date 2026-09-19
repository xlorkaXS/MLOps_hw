"""Небольшие проверки инвариантов, которых нет в сценарии check.sh."""

import unittest

from src.contamination import is_clean, report
from src.dedup import near_duplicates
from src.schema import Example
from src.split import group_split
from src.textnorm import question_for_near_dup


def example(index: int, topic: str, user: str) -> Example:
    return Example.model_validate(
        {
            "id": f"example-{index}",
            "topic": topic,
            "messages": [
                {"role": "system", "content": "Отвечай на вопрос."},
                {"role": "user", "content": user},
                {"role": "assistant", "content": f"Ответ на вопрос {index}."},
            ],
        }
    )


class PipelineRegressions(unittest.TestCase):
    def test_reordered_options_are_near_duplicates(self) -> None:
        question = "Тема: Климат. Вопрос: Какой прибор измеряет давление? Варианты ответа: "
        first = question + "0. термометр 1. барометр 2. гигрометр"
        second = question + "0. гигрометр 1. термометр 2. барометр"
        texts = [question_for_near_dup(first), question_for_near_dup(second)]
        self.assertEqual(near_duplicates(texts, 4, 64, 0.85), [1])

    def test_group_split_has_no_cross_split_contamination(self) -> None:
        rows = [
            example(i, f"Тема {i // 20}", f"Вопрос номер {i} в теме {i // 20}?")
            for i in range(1200)
        ]
        buckets = group_split(rows, {"train": 0.8, "val": 0.1, "test": 0.1}, 42)
        self.assertEqual({name: len(items) for name, items in buckets.items()},
                         {"train": 960, "val": 120, "test": 120})
        self.assertTrue(is_clean(report(buckets["train"], buckets["test"], 4, 64, 0.85)))


if __name__ == "__main__":
    unittest.main()
