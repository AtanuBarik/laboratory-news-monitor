#!/usr/bin/env python3
"""Email dispatcher that prefers cached ChatGPT summaries and uses Gemini only for gaps."""

from __future__ import annotations

import os
from typing import Any

import email_dispatch as base
import email_new_items as core


def truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


GEMINI_DISABLED = truthy(os.getenv("DISABLE_GEMINI", ""))
if GEMINI_DISABLED:
    core.WORKER = ""
    base.GEMINI_QUOTA_EXHAUSTED = True
    base.GEMINI_QUOTA_MESSAGE = "Gemini fallback is unavailable for this run."
    print("Gemini fallback disabled for this run; cached ChatGPT summaries are preferred and feed previews remain available as a final fallback.")


def cached_chatgpt_summary(item: dict[str, Any]) -> str:
    return core.clean_summary(str(item.get("chatgpt_summary") or ""))


def collect_hybrid(
    candidates: list[dict[str, Any]],
    target: int,
    max_attempts: int,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str], int]:
    selected: list[dict[str, Any]] = []
    summary_map: dict[str, str] = {}
    unreadable: list[str] = []
    attempted = 0
    chatgpt_count = 0
    gemini_count = 0
    limit = min(len(candidates), max_attempts)

    while len(selected) < target and attempted < limit:
        wave_size = 1 if attempted == 0 and not any(cached_chatgpt_summary(x) for x in candidates[:base.WAVE_SIZE]) else base.WAVE_SIZE
        wave = candidates[attempted : min(attempted + wave_size, limit)]
        attempted += len(wave)
        print("Hybrid summary wave:", ", ".join(f"{core.item_id(item)} ({item.get('title', '')[:70]})" for item in wave))

        wave_summaries: dict[str, str] = {}
        missing: list[dict[str, Any]] = []
        for item in wave:
            identifier = core.item_id(item)
            cached = cached_chatgpt_summary(item)
            if cached:
                wave_summaries[identifier] = cached
                chatgpt_count += 1
                print(f"Using scheduled ChatGPT summary for {identifier}: {len(cached.split())} words.")
            else:
                missing.append(item)

        if missing and core.WORKER and not base.GEMINI_QUOTA_EXHAUSTED:
            generated = core.verified_summaries(missing)
            wave_summaries.update(generated)
            gemini_count += len(generated)

        for item in wave:
            identifier = core.item_id(item)
            summary = wave_summaries.get(identifier)
            if summary and len(selected) < target:
                selected.append(item)
                summary_map[identifier] = summary
            elif not summary:
                unreadable.append(identifier)

        print(
            f"Wave result: {len(wave_summaries)} usable; report now has {len(selected)} of {target}. "
            f"Scheduled ChatGPT={chatgpt_count}, Gemini={gemini_count}."
        )
        if base.GEMINI_QUOTA_EXHAUSTED and not any(cached_chatgpt_summary(x) for x in candidates[attempted:limit]):
            break

    base.HYBRID_CHATGPT_COUNT = chatgpt_count
    base.HYBRID_GEMINI_COUNT = gemini_count
    return selected, summary_map, unreadable, attempted


base.collect_verified = collect_hybrid


def main() -> int:
    result = base.main()
    try:
        status = core.load(core.STATUS, {})
        status["scheduled_chatgpt_summary_count"] = int(getattr(base, "HYBRID_CHATGPT_COUNT", 0))
        status["gemini_summary_count"] = int(getattr(base, "HYBRID_GEMINI_COUNT", 0))
        status["gemini_fallback_enabled"] = bool(core.WORKER)
        if GEMINI_DISABLED:
            status["gemini_unavailable"] = True
            status["gemini_quota_exhausted"] = False
        if status.get("scheduled_chatgpt_summary_count"):
            status["primary_summary_provider"] = "ChatGPT Scheduled Task"
        elif status.get("gemini_summary_count"):
            status["primary_summary_provider"] = "Gemini API fallback"
        elif status.get("fallback_summary_count"):
            status["primary_summary_provider"] = "Feed/source preview fallback"
        core.save(core.STATUS, status)
    except Exception as exc:
        print(f"Could not append hybrid-provider status metadata: {exc}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
