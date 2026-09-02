"""Rush22 — daily Shorts pipeline.

Runs unattended from GitHub Actions twice a day. Every external call here
(Gemini, edge-tts, YouTube) can fail transiently, and a failure at 07:00 KST
has nobody watching it, so each one retries and the run records what
happened either way.

The ordering matters: the topic is only marked used once YouTube hands back
a video id. A run that dies mid-render leaves the topic available for the
next one instead of silently burning it.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

import edge_tts
from google import genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from moviepy import AudioFileClip, CompositeVideoClip, ImageClip

import topics
from render import (
    CAPTION_TOP,
    HEIGHT,
    WIDTH,
    Word,
    find_korean_font,
    make_background,
    make_hook_card,
    render_caption_frames,
)

ROOT = Path(__file__).resolve().parent
USED_LOG = ROOT / "used_log.csv"

LOG_FIELDS = [
    "date",
    "topic_id",
    "topic",
    "title",
    "youtube_video_id",
    "status",
    "duration_sec",
    "note",
]

VOICE = os.environ.get("RUSH_VOICE", "ko-KR-SunHiNeural")
VOICE_RATE = os.environ.get("RUSH_VOICE_RATE", "+8%")

MODEL = "gemini-3.6-flash"

# YouTube keeps anything over 3 minutes out of the Shorts feed, but the feed
# rewards far shorter than that. We aim for 35-45s and hard-stop well under
# the limit so a runaway script can never publish as a regular video.
MAX_DURATION = 58.0

# Gemini overshoots the length it is asked for. The first run came back at
# 52s against a 45s request, uncomfortably close to the cap, so the script
# is trimmed to a sentence boundary before it ever reaches the renderer.
SCRIPT_CHAR_LIMIT = 420

HOOK_SECONDS = 1.6


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def retry(times: int = 3, delay: float = 4.0):
    """Retry with linear backoff.

    Deliberately not exponential: the failures that actually show up here are
    brief edge-tts blips and Gemini rate limits, both of which clear in
    seconds. Long backoffs just push the job toward the runner timeout.
    """

    def decorator(fn):
        def wrapper(*args, **kwargs):
            last = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    print(f"[retry] {fn.__name__} {attempt}/{times} 실패: {exc}")
                    if attempt < times:
                        time.sleep(delay * attempt)
            raise last

        return wrapper

    return decorator


def strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def append_log(row: dict) -> None:
    is_new = not USED_LOG.exists() or USED_LOG.stat().st_size == 0
    with open(USED_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def migrate_log() -> None:
    """Bring the log up to the current column set.

    The original log had no status/duration/note columns. Rewriting it once
    stops DictWriter from dropping those fields and keeps the old rows
    readable by the same analysis.
    """
    if not USED_LOG.exists():
        return
    with open(USED_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or set(rows[0].keys()) == set(LOG_FIELDS):
        return
    for row in rows:
        row.setdefault("status", "uploaded" if row.get("youtube_video_id") else "unknown")
        row.setdefault("duration_sec", "")
        row.setdefault("note", "")
    with open(USED_LOG, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"[log] {len(rows)}행을 새 스키마로 이관")


# --------------------------------------------------------------------------
# script
# --------------------------------------------------------------------------


@retry(times=3)
def generate_script(client, topic: str) -> dict:
    """Write the narration.

    The prompt asks for a hook/body/payoff split rather than a blob of prose,
    because that structure is what holds a Short together: a question in the
    first second, one concrete mechanism in the middle, and a closing line
    worth rewatching.
    """
    prompt = f"""당신은 한국 개발자 대상 유튜브 쇼츠 작가입니다.

## 주제
{topic}

## 대본 규칙
- 공백 포함 **300~370자**. 이 범위를 넘기지 말 것 (낭독 시 약 35~43초)
- 구조: 후킹 질문 → 핵심 원리 설명 → 실무에서 뭐가 달라지는지
- 첫 문장은 반드시 질문이나 반전으로 시작 (스크롤을 멈추게 할 것)
- 구체적인 수치, 동작 원리, 실제 사례를 넣을 것
- "여러분", "오늘은 ~에 대해 알아보겠습니다" 같은 상투적 도입 금지
- 광고성 표현, 특정 제품 추천 금지
- 마지막 문장은 요약이 아니라 통찰 한 줄

## 출력 (JSON만, 백틱 금지)
{{
  "hook": "첫 화면에 크게 박힐 8~14자 문구",
  "title": "유튜브 제목 25~45자, 검색 키워드를 앞에 배치",
  "topic_label": "화면 상단 칩에 들어갈 8~14자 분야명",
  "script": "실제 낭독될 전체 대본",
  "summary": "설명란 첫 줄에 들어갈 한 문장 요약",
  "tags": ["검색 태그", "8~12개", "한글과 영문 혼합"]
}}
"""
    response = client.models.generate_content(model=MODEL, contents=prompt)
    data = json.loads(strip_fence(response.text))

    for key in ("hook", "title", "script", "summary"):
        if not data.get(key):
            raise ValueError(f"대본 필드 누락: {key}")

    data.setdefault("topic_label", "개발 트렌드")
    tags = [t for t in data.get("tags", []) if isinstance(t, str) and t.strip()]
    data["tags"] = (tags or ["개발자", "프로그래밍", "기술트렌드"])[:12]
    data["script"] = trim_to_sentence(data["script"], SCRIPT_CHAR_LIMIT)
    return data


def trim_to_sentence(script: str, limit: int) -> str:
    """Cut an overlong script back to its last complete sentence.

    The renderer also caps duration, but that cap lands mid-word on the
    audio. Trimming here instead means an overlong script ends on a finished
    thought rather than being sliced off in the middle of one.
    """
    script = script.strip()
    if len(script) <= limit:
        return script

    head = script[:limit]
    cut = max(head.rfind(c) for c in ".!?")
    if cut < limit * 0.5:
        # No sentence break in a usable place -- keep the whole thing and let
        # the duration cap handle it rather than ending mid-clause here.
        print(f"[script] {len(script)}자, 문장 경계를 못 찾아 그대로 진행")
        return script

    trimmed = head[: cut + 1]
    print(f"[script] {len(script)}자 → {len(trimmed)}자로 문장 단위 절단")
    return trimmed


# --------------------------------------------------------------------------
# narration
# --------------------------------------------------------------------------


def _communicate(text: str):
    """Build a Communicate that reports per-word timings.

    edge-tts 7.x defaults `boundary` to "SentenceBoundary", so asking for
    word timings is opt-in -- without this the stream carries no
    WordBoundary events at all and captions silently degrade to even
    spacing. Older releases have no such parameter and emit word events
    unconditionally, hence the TypeError path.
    """
    try:
        return edge_tts.Communicate(
            text, VOICE, rate=VOICE_RATE, boundary="WordBoundary"
        )
    except TypeError:
        return edge_tts.Communicate(text, VOICE, rate=VOICE_RATE)


async def synthesize(text: str, out_path: Path) -> list[Word]:
    """Render narration and capture per-word timings.

    edge-tts emits boundary events alongside the audio stream; those offsets
    are what make word-synced captions possible without a forced aligner.
    Offsets arrive in 100-nanosecond ticks.
    """
    audio = bytearray()
    words: list[Word] = []
    sentences: list[Word] = []

    async for chunk in _communicate(text).stream():
        kind = chunk["type"]
        if kind == "audio":
            audio.extend(chunk["data"])
        elif kind in ("WordBoundary", "SentenceBoundary"):
            start = chunk["offset"] / 10_000_000
            span = Word(chunk["text"], start, start + chunk["duration"] / 10_000_000)
            (words if kind == "WordBoundary" else sentences).append(span)

    if not audio:
        raise RuntimeError("edge-tts가 오디오를 반환하지 않았습니다")

    out_path.write_bytes(bytes(audio))

    if words:
        return words

    # Two fallbacks, both aimed at the same thing: never let a change on
    # Microsoft's side take the channel dark. Sentence spans still anchor
    # captions to real audio, so they are much closer than spreading words
    # evenly across the whole clip.
    if sentences:
        print("[tts] 단어 타이밍 없음 — 문장 구간 내 분배로 대체")
        return [w for s in sentences for w in estimate_words(s.text, s.end - s.start, s.start)]

    print("[tts] 경계 이벤트 없음 — 전체 균등 분배로 대체")
    return estimate_words(text, _audio_duration(out_path))


def _audio_duration(path: Path) -> float:
    from moviepy import AudioFileClip

    clip = AudioFileClip(str(path))
    try:
        return clip.duration
    finally:
        clip.close()


def estimate_words(text: str, total: float, offset: float = 0.0) -> list[Word]:
    """Spread tokens across a span, weighted by length.

    Used when edge-tts gives us audio but no word boundaries -- either
    across one sentence's measured span, or across the whole clip. Weighting
    by length keeps long tokens from flashing past at the same rate as short
    ones, which is what makes naive equal spacing look broken.
    """
    tokens = text.split()
    if not tokens or total <= 0:
        return []
    weights = [len(t) + 1.5 for t in tokens]
    scale = total / sum(weights)
    words, t = [], offset
    for token, weight in zip(tokens, weights):
        span = weight * scale
        words.append(Word(token, t, t + span))
        t += span
    return words


async def synthesize_with_retry(text: str, out_path: Path) -> list[Word]:
    last = None
    for attempt in range(1, 4):
        try:
            return await synthesize(text, out_path)
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[retry] TTS {attempt}/3 실패: {exc}")
            if attempt < 3:
                await asyncio.sleep(4 * attempt)
    raise last


# --------------------------------------------------------------------------
# video
# --------------------------------------------------------------------------


def build_video(
    audio_path: Path,
    words: list[Word],
    data: dict,
    seed: str,
    font_path: str,
    workdir: Path,
    out_path: Path,
) -> float:
    audio = AudioFileClip(str(audio_path))
    duration = audio.duration
    if duration > MAX_DURATION:
        print(f"[video] 대본이 길어 {MAX_DURATION}초로 절단 (원본 {duration:.1f}s)")
        audio = audio.subclipped(0, MAX_DURATION)
        duration = MAX_DURATION

    # The background is rendered oversized and slid across the frame. Panning
    # a larger still costs far less than a per-frame resize, and at this speed
    # it reads as a deliberate drift rather than motion for its own sake.
    bg_path = workdir / "bg.png"
    make_background(data["topic_label"], data["hook"], seed, font_path).save(bg_path)
    bg_img = ImageClip(str(bg_path))
    span_x = bg_img.w - WIDTH
    span_y = bg_img.h - HEIGHT
    background = bg_img.with_duration(duration).with_position(
        lambda t: (
            -span_x * (0.5 + 0.5 * (t / duration)),
            -span_y * (0.5 - 0.5 * (t / duration)),
        )
    )

    layers = [background]

    # Opening card, held just long enough to read before the captions take
    # over mid-sentence.
    hook_path = workdir / "hook.png"
    make_hook_card(data["hook"], seed, font_path).save(hook_path)
    layers.append(
        ImageClip(str(hook_path)).with_duration(min(HOOK_SECONDS, duration)).with_start(0)
    )

    frames = render_caption_frames(words, font_path, seed, workdir / "caps")
    print(f"[video] 자막 프레임 {len(frames)}개")

    for path, start, end in frames:
        if start >= duration:
            break
        clip_end = min(end, duration)
        if clip_end - start < 0.04:
            continue
        layers.append(
            ImageClip(str(path))
            .with_duration(clip_end - start)
            .with_start(start)
            .with_position((0, CAPTION_TOP))
        )

    video = CompositeVideoClip(layers, size=(WIDTH, HEIGHT)).with_audio(audio)
    video.write_videofile(
        str(out_path),
        fps=24,
        codec="libx264",
        audio_codec="aac",
        preset="veryfast",
        threads=4,
        logger=None,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    )
    video.close()
    audio.close()
    return duration


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------


def build_description(data: dict, topic: str) -> str:
    """Per-video description.

    The old pipeline sent the same three lines on every upload. Identical
    metadata across a catalogue is one of the clearest mass-production
    signals YouTube looks for, and it wastes the strongest on-page search
    surface a Short has.
    """
    tags = [re.sub(r"[^0-9A-Za-z가-힣]", "", t) for t in data["tags"][:6]]
    hashtags = " ".join(f"#{t}" for t in tags if t)
    return "\n".join(
        [
            data["summary"],
            "",
            f"오늘 다룬 주제: {topic}",
            "",
            "개발자가 알아두면 실무가 달라지는 기술 이야기를 하루 두 번 올립니다.",
            "구독해두면 출근길에 하나씩 챙겨볼 수 있습니다.",
            "",
            f"{hashtags} #개발자 #Shorts",
            "",
            "※ 내레이션과 대본 구성에 생성형 AI를 활용했으며, 주제 선정과 검수는 직접 합니다.",
        ]
    )


def build_title(data: dict) -> str:
    """Title capped to YouTube's 100-character limit with #Shorts preserved."""
    title = data["title"].strip()
    suffix = " #Shorts"
    if len(title) + len(suffix) > 100:
        title = title[: 100 - len(suffix) - 1].rstrip() + "…"
    return title + suffix


@retry(times=3, delay=6.0)
def upload(video_path: Path, data: dict, topic: str) -> str:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": build_title(data),
            "description": build_description(data, topic),
            "tags": data["tags"],
            "categoryId": "28",
            "defaultAudioLanguage": "ko",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


async def run() -> int:
    migrate_log()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    font_path = find_korean_font()

    topic_item = topics.pick_next(client)
    topic = topic_item["topic"]
    print(f"[1/5] 주제: {topic_item['id']} — {topic}")

    today = datetime.date.today().isoformat()

    def fail(stage: str, exc: Exception) -> int:
        """Record the failure without consuming the topic.

        No video id means `topics.load_used_ids` skips this row, so the next
        run picks the same topic up again instead of losing it.
        """
        traceback.print_exc()
        append_log(
            {
                "date": today,
                "topic_id": topic_item["id"],
                "topic": topic,
                "title": "",
                "youtube_video_id": "",
                "status": f"failed:{stage}",
                "duration_sec": "",
                "note": str(exc)[:200],
            }
        )
        print(f"\n[실패] {stage}: {exc}")
        return 1

    try:
        data = generate_script(client, topic)
    except Exception as exc:  # noqa: BLE001
        return fail("script", exc)
    print(f"[2/5] 제목: {data['title']}")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        audio_path = workdir / "voice.mp3"
        video_path = workdir / "short.mp4"

        try:
            words = await synthesize_with_retry(data["script"], audio_path)
        except Exception as exc:  # noqa: BLE001
            return fail("tts", exc)
        print(f"[3/5] 내레이션 {len(words)}단어")

        if not words:
            return fail("tts", RuntimeError("단어 타이밍이 비어 있습니다"))

        try:
            duration = build_video(
                audio_path, words, data, topic_item["id"], font_path, workdir, video_path
            )
        except Exception as exc:  # noqa: BLE001
            return fail("render", exc)
        size_mb = video_path.stat().st_size / 1_048_576
        print(f"[4/5] 영상 {duration:.1f}초 / {size_mb:.1f}MB")

        try:
            video_id = upload(video_path, data, topic)
        except Exception as exc:  # noqa: BLE001
            return fail("upload", exc)

    append_log(
        {
            "date": today,
            "topic_id": topic_item["id"],
            "topic": topic,
            "title": data["title"],
            "youtube_video_id": video_id,
            "status": "uploaded",
            "duration_sec": f"{duration:.1f}",
            "note": "",
        }
    )
    print(f"[5/5] 완료 — https://youtube.com/shorts/{video_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
