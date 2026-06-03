from __future__ import annotations

from typing import Any


FILLERS = ("haan ji", "bilkul", "achha", "theek hai", "ek second", "samajh gaya")


def default_metrics() -> dict[str, Any]:
    return {
        "total_turns": 0,
        "assistant_turns": 0,
        "user_turns": 0,
        "repeated_questions": 0,
        "known_facts_used": 0,
        "known_facts_repeated": 0,
        "objections_detected": 0,
        "site_visit_attempts": 0,
        "whatsapp_offer_attempts": 0,
        "average_response_words": 0.0,
        "average_response_latency": 0.0,
        "filler_usage_count": 0,
        "question_count": 0,
        "questions_per_turn": 0.0,
        "robotic_behavior_detected": False,
    }


def merge_metrics(memory: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    metrics = default_metrics()
    metrics.update(memory.get("conversation_metrics") or {})
    metrics.update(updates)
    return metrics


def user_turn_metrics(memory: dict[str, Any], text: str, objections: list[str] | None = None) -> dict[str, Any]:
    metrics = default_metrics()
    metrics.update(memory.get("conversation_metrics") or {})
    metrics["total_turns"] += 1
    metrics["user_turns"] += 1
    if objections:
        metrics["objections_detected"] += len(objections)
    return metrics


def assistant_turn_metrics(memory: dict[str, Any], text: str, *, response_latency_ms: float | None = None) -> dict[str, Any]:
    metrics = default_metrics()
    metrics.update(memory.get("conversation_metrics") or {})
    lowered = str(text or "").lower()
    words = lowered.split()
    word_count = len(words)
    assistant_turns = int(metrics.get("assistant_turns") or 0) + 1
    previous_avg = float(metrics.get("average_response_words") or 0)
    metrics["assistant_turns"] = assistant_turns
    metrics["total_turns"] += 1
    metrics["average_response_words"] = round(((previous_avg * (assistant_turns - 1)) + word_count) / assistant_turns, 2)
    if response_latency_ms is not None:
        previous_latency = float(metrics.get("average_response_latency") or 0)
        metrics["average_response_latency"] = round(
            ((previous_latency * (assistant_turns - 1)) + response_latency_ms) / assistant_turns,
            2,
        )
    metrics["filler_usage_count"] += sum(1 for filler in FILLERS if filler in lowered)
    questions = lowered.count("?")
    metrics["question_count"] += questions
    metrics["questions_per_turn"] = round(metrics["question_count"] / assistant_turns, 2)
    if "visit" in lowered or "site" in lowered:
        metrics["site_visit_attempts"] += 1
    if "whatsapp" in lowered:
        metrics["whatsapp_offer_attempts"] += 1
    if questions > 2 or word_count > 35:
        metrics["robotic_behavior_detected"] = True
    return metrics

