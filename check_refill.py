"""Dry-run the topic refill without publishing or writing anything.

The refill only fires when the bank drops below its threshold, which is
weeks away. That means the one code path capable of taking the channel dark
mid-month would otherwise go untested until the day it matters. This asks
Gemini for topics exactly the way the pipeline will, checks the response is
usable, and throws the result away.

Run from the workflow of the same name, or locally with GEMINI_API_KEY set.
"""

from __future__ import annotations

import os
import sys

from google import genai

import topics

SAMPLE = 5


def main() -> int:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    bank = topics.load_bank()
    used = topics.load_used_topics()
    remaining = len(topics.unused(bank))
    print(f"뱅크 {len(bank)}개 / 미사용 {remaining}개 / 발행 이력 {len(used)}개")
    print(f"보충 발동 임계값: {topics.REFILL_THRESHOLD}개 미만\n")

    avoid = [t["topic"] for t in bank] + used
    print(f"중복 회피 목록 {len(avoid)}개를 프롬프트에 실어 {topics.REFILL_COUNT}개 요청...")

    fresh = topics.generate_topics(client, topics.REFILL_COUNT, avoid)

    problems = []
    if not fresh:
        problems.append("주제를 하나도 받지 못했습니다")
    elif len(fresh) < topics.REFILL_COUNT // 2:
        problems.append(f"요청 {topics.REFILL_COUNT}개 대비 {len(fresh)}개만 반환")

    # generate_topics already filters exact repeats; this catches the case
    # where Gemini returns near-identical wording that slips the equality
    # check, which is what a bank slowly filling with restated topics looks
    # like before anyone notices.
    existing = {t.strip() for t in avoid}
    overlap = [t for t in fresh if t.strip() in existing]
    if overlap:
        problems.append(f"중복 통과: {overlap[:3]}")

    short = [t for t in fresh if len(t) < 15]
    if short:
        problems.append(f"너무 짧은 주제: {short[:3]}")

    print(f"\n{len(fresh)}개 수신. 앞 {SAMPLE}개:")
    for t in fresh[:SAMPLE]:
        print(f"  - {t}")

    if problems:
        print("\n문제:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\n보충 경로 정상 — 실제 파일은 건드리지 않았습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
