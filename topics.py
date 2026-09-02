"""Topic bank management.

The bank used to fall back to `bank[0]` once every topic was used, which
meant that from day 30 onward the channel re-uploaded T001 forever. Under
YouTube's inauthentic-content policy that is the worst possible failure
mode: it looks exactly like a bot stuck in a loop. So the bank refills
itself instead, and the refill prompt carries the full history so Gemini
cannot hand back a topic that already aired.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOPIC_BANK = ROOT / "topic_bank.json"
USED_LOG = ROOT / "used_log.csv"

# Refill before the bank runs dry. If we waited for zero, a single failed
# Gemini call would leave the next run with nothing to publish.
REFILL_THRESHOLD = 12
REFILL_COUNT = 40


def load_bank() -> list[dict]:
    if not TOPIC_BANK.exists():
        return []
    return json.loads(TOPIC_BANK.read_text(encoding="utf-8"))


def save_bank(bank: list[dict]) -> None:
    TOPIC_BANK.write_text(
        json.dumps(bank, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_used_ids() -> set[str]:
    """Topic ids that have already been published.

    Only rows that actually reached YouTube count. A run that died during
    render must not burn its topic, otherwise a bad week silently eats the
    bank.
    """
    if not USED_LOG.exists():
        return set()
    with open(USED_LOG, newline="", encoding="utf-8") as f:
        return {
            row["topic_id"]
            for row in csv.DictReader(f)
            if row.get("topic_id") and row.get("youtube_video_id")
        }


def load_used_topics() -> list[str]:
    if not USED_LOG.exists():
        return []
    with open(USED_LOG, newline="", encoding="utf-8") as f:
        return [row["topic"] for row in csv.DictReader(f) if row.get("topic")]


def next_id(bank: list[dict]) -> str:
    highest = 0
    for item in bank:
        m = re.match(r"T(\d+)$", item.get("id", ""))
        if m:
            highest = max(highest, int(m.group(1)))
    return f"T{highest + 1:03d}"


def unused(bank: list[dict]) -> list[dict]:
    used = load_used_ids()
    return [t for t in bank if t["id"] not in used]


def generate_topics(client, count: int, avoid: list[str]) -> list[dict]:
    """Ask Gemini for fresh topics, given everything already covered.

    `avoid` is the full history, not a sample. Truncating it is how you end
    up re-publishing something from two months ago.
    """
    avoid_block = "\n".join(f"- {t}" for t in avoid) if avoid else "(없음)"
    prompt = f"""당신은 한국 개발자 대상 유튜브 채널의 콘텐츠 기획자입니다.
30초 쇼츠로 만들 기술 주제 {count}개를 새로 제안하세요.

## 이미 다룬 주제 (절대 중복 금지, 유사 주제도 제외)
{avoid_block}

## 조건
- 현업 개발자가 "이건 몰랐다"고 느낄 구체적인 주제
- 언어/프레임워크/인프라/AI/보안/성능 등 영역을 고루 분산
- "~하는 이유", "~와 ~의 차이", "~할 때 흔한 실수" 같은 구체적 각도
- 너무 광범위한 주제 금지 (예: "파이썬 배우기" X, "파이썬 GIL이 멀티스레딩을 막는 방식" O)
- 한 줄 30~60자

## 출력
JSON 배열만 출력. 마크다운 백틱 금지.
[{{"topic": "주제 문장"}}, ...]
"""
    response = client.models.generate_content(
        model="gemini-3.6-flash", contents=prompt
    )
    items = json.loads(_strip_fence(response.text))

    bank = load_bank()
    seen = {t["topic"] for t in bank} | set(avoid)
    fresh = []
    for item in items:
        topic = (item.get("topic") or "").strip()
        if topic and topic not in seen:
            seen.add(topic)
            fresh.append(topic)
    return fresh


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def refill_if_needed(client) -> bool:
    """Top the bank up when it runs low. Returns True if it was written."""
    bank = load_bank()
    remaining = len(unused(bank))
    if remaining >= REFILL_THRESHOLD:
        return False

    print(f"[topics] 남은 주제 {remaining}개 — {REFILL_COUNT}개 보충 시도")
    avoid = [t["topic"] for t in bank] + load_used_topics()

    try:
        fresh = generate_topics(client, REFILL_COUNT, avoid)
    except Exception as exc:  # noqa: BLE001 - refill must never break publishing
        print(f"[topics] 보충 실패 (계속 진행): {exc}")
        return False

    if not fresh:
        print("[topics] 새 주제를 얻지 못함")
        return False

    for topic in fresh:
        bank.append({"id": next_id(bank), "topic": topic})
    save_bank(bank)
    print(f"[topics] {len(fresh)}개 추가 (총 {len(bank)}개)")
    return True


def pick_next(client) -> dict:
    """The next topic to publish, refilling the bank first if it is low."""
    refill_if_needed(client)
    remaining = unused(load_bank())
    if not remaining:
        raise RuntimeError(
            "발행할 주제가 없고 보충도 실패했습니다. "
            "GEMINI_API_KEY와 topic_bank.json을 확인하세요."
        )
    return remaining[0]
