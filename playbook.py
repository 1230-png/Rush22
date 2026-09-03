"""The channel's content design system.

Six axes decide what a video looks and sounds like. They are collected here
rather than buried in a prompt string so the whole editorial stance is one
readable file, and so changing the channel's direction is an edit to data
instead of a rewrite of the pipeline.

    1. 구조  BEATS      — how the 40 seconds are divided
    2. 포맷  FORMATS    — which of six video shapes this one is
    3. 편집  (render.py) — the visual rhythm those beats drive
    4. 주제  (topics.py) — what gets covered
    5. 가치  VALUE_RULES — what the viewer leaves with
    6. 훅    HOOK_RULES  — the first second

Formats rotate deterministically by publish count. Letting the model pick
would quietly collapse onto whichever shape it likes best, and a catalogue
of one shape is the "mass-produced" signal that gets a channel rejected at
review.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# 1. 구조 — the beats every script is built from
# --------------------------------------------------------------------------

# weight drives the character budget, so the middle of the video carries the
# actual substance and the ending does not trail off into filler.
BEATS = [
    ("hook", 1, "스크롤을 멈추게 하는 한 줄. 질문·모순·숫자 중 하나."),
    ("tension", 2, "왜 이게 문제인지, 혹은 뭐가 의외인지. 여기서 궁금해져야 한다."),
    ("payoff", 4, "실제 동작 원리나 메커니즘. 이 영상의 알맹이."),
    ("apply", 2, "그래서 내일 코드에서 뭐가 달라지는지."),
]

TOTAL_WEIGHT = sum(w for _, w, _ in BEATS)

# Where the on-screen top line switches from the hook to the core insight.
# Placing it at the payoff boundary gives one deliberate visual change at the
# moment the video starts paying out.
SWITCH_AT_BEAT = "payoff"


# --------------------------------------------------------------------------
# 2. 포맷 — six shapes, rotated so no two consecutive videos match
# --------------------------------------------------------------------------

FORMATS = [
    {
        "id": "principle",
        "name": "원리형",
        "chip": "동작 원리",
        "guide": (
            "겉으로 보이는 현상에서 시작해 내부 동작 원리까지 파고든다. "
            "'왜 이렇게 되는가'에 답할 것. 추상적 설명 금지, 실제 메커니즘을 말할 것."
        ),
        "title_shape": "「X가 Y하는 진짜 이유」 형태",
    },
    {
        "id": "compare",
        "name": "비교형",
        "chip": "선택 기준",
        "guide": (
            "비슷해 보이는 두 기술·방식을 놓고 언제 무엇을 써야 하는지 가른다. "
            "'A가 더 좋다'가 아니라 '어떤 조건에서 갈리는지'를 말할 것."
        ),
        "title_shape": "「A vs B, 언제 뭘 쓰나」 형태",
    },
    {
        "id": "mistake",
        "name": "실수형",
        "chip": "흔한 실수",
        "guide": (
            "현업에서 자주 잘못 쓰는 방식을 짚고 왜 틀렸는지 설명한다. "
            "비난 조가 아니라 '그럴 만한 이유가 있었다'는 톤으로."
        ),
        "title_shape": "「~할 때 흔히 놓치는 것」 형태",
    },
    {
        "id": "number",
        "name": "숫자형",
        "chip": "핵심 정리",
        "guide": (
            "핵심을 2~3개로 끊어 제시한다. 나열로 끝내지 말고 "
            "각 항목이 왜 그 순서인지가 드러나야 한다."
        ),
        "title_shape": "「~하는 N가지 방법」 형태",
    },
    {
        "id": "myth",
        "name": "반전형",
        "chip": "통념 점검",
        "guide": (
            "널리 믿어지는 통념을 뒤집는다. 통념을 먼저 정확히 요약한 뒤 "
            "어디서부터 틀렸는지 짚을 것. 낚시성 반전 금지."
        ),
        "title_shape": "「사실 X는 ~가 아닙니다」 형태",
    },
    {
        "id": "case",
        "name": "사례형",
        "chip": "실제 사례",
        "guide": (
            "실제 장애·사고·유명 서비스 사례에서 교훈을 뽑는다. "
            "사례를 특정할 수 없으면 지어내지 말고 일반적인 상황으로 서술할 것."
        ),
        "title_shape": "「~해서 생긴 일」 형태",
    },
]


def pick_format(published_count: int) -> dict:
    """Rotate formats by how many videos have shipped.

    Deterministic on purpose: it guarantees all six shapes cycle evenly, and
    it makes the format of any given video reproducible from the log alone.
    """
    return FORMATS[published_count % len(FORMATS)]


# --------------------------------------------------------------------------
# 5. 가치 / 6. 훅 — the two rules the script is checked against
# --------------------------------------------------------------------------

VALUE_RULES = """- 시청자가 가져갈 것이 반드시 하나 있어야 한다: 구체적인 수치, 동작 원리,
  혹은 당장 바꿀 수 있는 판단 기준
- "중요합니다", "알아두면 좋습니다" 같은 공허한 문장 금지
- 검색하면 첫 줄에 나오는 수준의 정보만 담지 말 것
- 특정 제품 추천이나 성능 보장으로 읽힐 표현 금지"""

HOOK_RULES = """- 8~14자. 화면 상단에 크게 박힌다
- 질문, 모순, 숫자 중 하나로 만들 것
- "~에 대해 알아봅시다" 같은 예고 금지 — 훅은 예고가 아니라 미끼다
- 본문에서 답이 나오지 않는 낚시 금지"""


def build_prompt(topic: str, fmt: dict, char_budget: int) -> str:
    """Assemble the script prompt for one video.

    The beat budget is spelled out per beat instead of given as one total,
    because a single number lets the model spend everything on setup and
    leave the payoff — the only part worth watching — as one rushed line.
    """
    per_beat = "\n".join(
        f'  - {name}: 약 {round(char_budget * weight / TOTAL_WEIGHT)}자 — {desc}'
        for name, weight, desc in BEATS
    )

    return f"""당신은 한국 개발자 대상 유튜브 쇼츠 작가입니다.

## 주제
{topic}

## 이번 편의 포맷: {fmt['name']}
{fmt['guide']}
제목은 {fmt['title_shape']}로 잡되, 어색하면 자연스러움을 우선하세요.

## 구조 (이 순서와 분량을 지킬 것)
{per_beat}

## 훅 규칙
{HOOK_RULES}

## 가치 규칙
{VALUE_RULES}

## 문체
- 구어체 존댓말. "여러분", "오늘은 ~에 대해" 같은 상투적 도입 금지
- 한 문장은 짧게. 쇼츠는 귀로 듣는 매체다
- 마지막 문장은 요약이 아니라 통찰 한 줄

## 출력 (JSON만, 백틱 금지)
{{
  "hook": "상단에 박힐 8~14자",
  "core": "영상 후반부 상단에 띄울 핵심 한 줄 (12~20자)",
  "title": "유튜브 제목 25~45자, 검색 키워드를 앞에 배치",
  "topic_label": "화면 칩에 들어갈 8~14자 분야명",
  "beats": {{
    "hook": "낭독될 문장",
    "tension": "낭독될 문장",
    "payoff": "낭독될 문장들",
    "apply": "낭독될 문장"
  }},
  "keywords": ["자막에서 강조할 핵심 용어", "2~5개", "본문에 실제로 등장하는 표기 그대로"],
  "summary": "설명란 첫 줄 한 문장",
  "tags": ["검색 태그", "8~12개"]
}}"""
