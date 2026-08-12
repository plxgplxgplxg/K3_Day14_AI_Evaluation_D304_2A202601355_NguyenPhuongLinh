from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase
from trulens.core import Feedback

from template import RAGASEvaluator, rerank_by_overlap


ROOT = Path(__file__).resolve().parent
GOLDEN_PATH = ROOT / "golden_dataset.json"
ANSWERS_PATH = ROOT / "artifacts" / "actual_answers.json"
OUTPUT_PATH = ROOT / "artifacts" / "bonus_analysis.json"

FRAMEWORK_CASE_IDS = ["E01", "M02", "H04", "A01", "A03"]
RERANK_CASE_IDS = ["H04", "M02", "M07", "E01", "A01"]


def _load_inputs() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    golden_raw = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    answers_raw = json.loads(ANSWERS_PATH.read_text(encoding="utf-8"))
    golden = {row["id"]: row for row in golden_raw["qa_pairs"]}
    answers = {row["id"]: row for row in answers_raw["answers"]}
    return golden, answers


def _contexts(answer_row: dict[str, Any]) -> list[str]:
    return [chunk["text"] for chunk in answer_row["retrieved_contexts"]]


class _HeuristicDeepEvalMetric(BaseMetric):
    def __init__(self, name: str, threshold: float, scorer):
        self._name = name
        self.threshold = threshold
        self._scorer = scorer
        self.score = None
        self.reason = None
        self.success = None
        self.error = None
        self.include_reason = True
        self.async_mode = False
        self.verbose_mode = False
        self.strict_mode = False
        self.flaky = False
        self.evaluation_model = "deterministic-heuristic"

    @property
    def __name__(self) -> str:
        return self._name

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        self.score = float(self._scorer(test_case))
        self.reason = f"{self._name} computed from deterministic overlap heuristic."
        self.success = self.score >= self.threshold
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case, *args, **kwargs)


def _run_deepeval_case(
    evaluator: RAGASEvaluator,
    question: str,
    expected: str,
    answer: str,
    contexts: list[str],
) -> dict[str, float]:
    case = LLMTestCase(
        input=question,
        actual_output=answer,
        expected_output=expected,
        retrieval_context=contexts,
    )
    metrics = [
        _HeuristicDeepEvalMetric(
            "AnswerRelevancy",
            0.5,
            lambda tc: evaluator.evaluate_relevance(tc.actual_output, tc.input),
        ),
        _HeuristicDeepEvalMetric(
            "ContextualRecall",
            0.5,
            lambda tc: evaluator.evaluate_context_recall(
                list(tc.retrieval_context or []), tc.expected_output or ""
            ),
        ),
        _HeuristicDeepEvalMetric(
            "ContextualPrecision",
            0.5,
            lambda tc: evaluator.evaluate_context_precision(
                list(tc.retrieval_context or []), tc.expected_output or ""
            ),
        ),
    ]
    return {metric.__name__: metric.measure(case) for metric in metrics}


def _run_trulens_case(
    evaluator: RAGASEvaluator,
    question: str,
    expected: str,
    answer: str,
    contexts: list[str],
) -> dict[str, float]:
    metrics = {
        "AnswerRelevancy": Feedback(
            lambda question, answer: evaluator.evaluate_relevance(answer, question)
        ),
        "ContextualRecall": Feedback(
            lambda contexts, expected: evaluator.evaluate_context_recall(contexts, expected)
        ),
        "ContextualPrecision": Feedback(
            lambda contexts, expected: evaluator.evaluate_context_precision(contexts, expected)
        ),
    }
    scores: dict[str, float] = {}
    scores["AnswerRelevancy"] = float(
        metrics["AnswerRelevancy"].run(question=question, answer=answer).result
    )
    scores["ContextualRecall"] = float(
        metrics["ContextualRecall"].run(contexts=contexts, expected=expected).result
    )
    scores["ContextualPrecision"] = float(
        metrics["ContextualPrecision"].run(contexts=contexts, expected=expected).result
    )
    return scores


def _framework_comparison(
    evaluator: RAGASEvaluator,
    golden: dict[str, dict[str, Any]],
    answers: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    per_case: list[dict[str, Any]] = []
    for case_id in FRAMEWORK_CASE_IDS:
        gold = golden[case_id]
        answer_row = answers[case_id]
        contexts = _contexts(answer_row)
        deepeval_scores = _run_deepeval_case(
            evaluator,
            gold["question"],
            gold["expected_answer"],
            answer_row["actual_answer"],
            contexts,
        )
        trulens_scores = _run_trulens_case(
            evaluator,
            gold["question"],
            gold["expected_answer"],
            answer_row["actual_answer"],
            contexts,
        )
        per_case.append(
            {
                "id": case_id,
                "deepeval": deepeval_scores,
                "trulens": trulens_scores,
            }
        )

    aggregate = {}
    for metric_name in ["AnswerRelevancy", "ContextualRecall", "ContextualPrecision"]:
        deepeval_values = [row["deepeval"][metric_name] for row in per_case]
        trulens_values = [row["trulens"][metric_name] for row in per_case]
        aggregate[metric_name] = {
            "deepeval_avg": mean(deepeval_values),
            "trulens_avg": mean(trulens_values),
            "max_abs_delta": max(
                abs(d - t) for d, t in zip(deepeval_values, trulens_values, strict=True)
            ),
        }

    return {
        "selected_case_ids": FRAMEWORK_CASE_IDS,
        "per_case": per_case,
        "aggregate": aggregate,
        "notes": {
            "deepeval_builtin_openrouter_probe": (
                "Attempted separately with OpenRouterModel and built-in "
                "AnswerRelevancyMetric; run was not used for the table because "
                "the provider call did not complete reliably in this environment."
            )
        },
    }


def _rerank_analysis(
    evaluator: RAGASEvaluator,
    golden: dict[str, dict[str, Any]],
    answers: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    selected_rows: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for case_id, answer_row in answers.items():
        gold = golden[case_id]
        contexts = _contexts(answer_row)
        reranked = rerank_by_overlap(contexts, gold["question"])
        row = {
            "id": case_id,
            "recall_before": evaluator.evaluate_context_recall(contexts, gold["expected_answer"]),
            "recall_after": evaluator.evaluate_context_recall(reranked, gold["expected_answer"]),
            "precision_before": evaluator.evaluate_context_precision(contexts, gold["expected_answer"]),
            "precision_after": evaluator.evaluate_context_precision(reranked, gold["expected_answer"]),
        }
        row["delta_precision"] = row["precision_after"] - row["precision_before"]
        all_rows.append(row)
        if case_id in RERANK_CASE_IDS:
            selected_rows.append(row)

    selected_rows.sort(key=lambda row: RERANK_CASE_IDS.index(row["id"]))
    return {
        "selected_case_ids": RERANK_CASE_IDS,
        "selected_rows": selected_rows,
        "all_case_summary": {
            "avg_recall_before": mean(row["recall_before"] for row in all_rows),
            "avg_recall_after": mean(row["recall_after"] for row in all_rows),
            "avg_precision_before": mean(row["precision_before"] for row in all_rows),
            "avg_precision_after": mean(row["precision_after"] for row in all_rows),
            "cases_improved": [row["id"] for row in all_rows if row["delta_precision"] > 0],
            "cases_unchanged": sum(1 for row in all_rows if row["delta_precision"] == 0),
        },
    }


def main() -> None:
    golden, answers = _load_inputs()
    evaluator = RAGASEvaluator()
    output = {
        "framework_comparison": _framework_comparison(evaluator, golden, answers),
        "reranking": _rerank_analysis(evaluator, golden, answers),
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
