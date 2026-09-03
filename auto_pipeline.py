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

import playbook
import topics
from render import (
    CAPTION_TOP,
    HEIGHT,
    SAFE_BOTTOM,
    WIDTH,
    Word,
    find_korean_font,
    make_background,
    make_hook_card,
    make_progress_frames,
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

# Tried in order. Google retires models for new callers without warning --
# that is exactly how every scheduled run from 8/25 onward died with a 404
# until someone noticed. A list means a retirement costs one wasted call
# instead of taking the channel down until a human intervenes.
MODELS = [
    os.environ.get("RUSH_MODEL", "gemini-3.6-flash"),
    "gemini-2.5-flash",
    "gemini-flash-latest",
]

# YouTube keeps anything over 3 minutes out of the Shorts feed, but the feed
# rewards far shorter than that. We aim for 35-45s and hard-stop well under
# the limit so a runaway script can never publish as a regular video.
MAX_DURATION = 58.0

# Gemini does not hold to a character count: published runs came back at
# 44.6s, 46.5s, 52.1s and 55.9s against the same budget. 360 puts the median
# near 45s, and fit_beats enforces the ceiling so the spread cannot reach
# MAX_DURATION -- where the cut lands mid-word.
SCRIPT_CHAR_LIMIT = 360

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


def call_model(client, prompt: str) -> str:
    """Ask Gemini, walking the model list and retrying overload.

    Two failure shapes, two responses. A 404 means the model is gone for
    good, so move to the next one immediately. A 503/429 means the model is
    busy, so wait -- and wait properly: the first 503 we hit in production
    burned all three attempts in twelve seconds and lost the slot.
    """
    last: Exception | None = None

    for model in MODELS:
        for attempt in range(1, 4):
            try:
                return client.models.generate_content(
                    model=model, contents=prompt
                ).text
            except Exception as exc:  # noqa: BLE001
                last = exc
                text = str(exc)
                if "404" in text or "NOT_FOUND" in text:
                    print(f"[model] {model} 사용 불가 — 다음 모델로")
                    break
                if "503" in text or "429" in text or "UNAVAILABLE" in text:
                    wait = 20 * attempt
                    print(f"[model] {model} 과부하 {attempt}/3 — {wait}초 대기")
                    if attempt < 3:
                        time.sleep(wait)
                    continue
                print(f"[model] {model} 오류 {attempt}/3: {exc}")
                if attempt < 3:
                    time.sleep(5 * attempt)

    raise RuntimeError(f"모든 모델 실패: {last}")


def generate_script(client, topic: str, published: int) -> dict:
    """Write one video against the six-axis playbook.

    The format rotates by publish count rather than being chosen per topic,
    so the catalogue cycles through all six shapes instead of collapsing onto
    whichever one the model finds easiest.
    """
    fmt = playbook.pick_format(published)
    print(f"[script] 포맷: {fmt['name']} ({fmt['id']})")

    prompt = playbook.build_prompt(topic, fmt, SCRIPT_CHAR_LIMIT)
    data = json.loads(strip_fence(call_model(client, prompt)))

    for key in ("hook", "title", "summary", "beats"):
        if not data.get(key):
            raise ValueError(f"대본 필드 누락: {key}")

    beats = data["beats"]
    missing = [name for name, _, _ in playbook.BEATS if not beats.get(name)]
    if missing:
        raise ValueError(f"비트 누락: {missing}")

    # Narration is the beats in order. Keeping them separate through to here
    # is what lets the renderer know when the payoff starts.
    data["beat_texts"] = fit_beats(
        [beats[name].strip() for name, _, _ in playbook.BEATS], SCRIPT_CHAR_LIMIT
    )
    data["script"] = " ".join(data["beat_texts"])
    data["format"] = fmt["id"]
    data.setdefault("topic_label", fmt["chip"])
    data.setdefault("core", data["hook"])

    keywords = [k for k in data.get("keywords", []) if isinstance(k, str) and k.strip()]
    data["keywords"] = tuple(k.strip() for k in keywords[:5])

    tags = [t for t in data.get("tags", []) if isinstance(t, str) and t.strip()]
    data["tags"] = (tags or ["개발자", "프로그래밍", "기술트렌드"])[:12]

    return data


def fit_beats(beat_texts: list[str], limit: int) -> list[str]:
    """Trim the narration to budget, cutting inside the payoff beat.

    The payoff is the only beat with several sentences, so it is the only
    place a cut lands cleanly. The hook is the reason anyone stayed and the
    closing line is the takeaway, so neither is touched -- an overlong script
    loses some of its middle rather than its point.

    Falls through untouched when there is no sensible sentence break, leaving
    MAX_DURATION as the backstop.
    """
    total = sum(len(t) for t in beat_texts) + max(len(beat_texts) - 1, 0)
    if total <= limit:
        return beat_texts

    names = [name for name, _, _ in playbook.BEATS]
    idx = names.index("payoff")
    payoff = beat_texts[idx]
    target = len(payoff) - (total - limit)

    if target < 60:
        print(f"[script] {total}자 — 잘라낼 여지가 없어 길이 캡에 맡김")
        return beat_texts

    cut = max(payoff[:target].rfind(c) for c in ".!?")
    if cut < target * 0.4:
        print(f"[script] {total}자 — 문장 경계를 못 찾아 길이 캡에 맡김")
        return beat_texts

    trimmed = list(beat_texts)
    trimmed[idx] = payoff[: cut + 1]
    new_total = sum(len(t) for t in trimmed) + len(trimmed) - 1
    print(f"[script] {total}자 → {new_total}자 (해소 비트에서 절단)")
    return trimmed


def payoff_word_index(data: dict) -> int:
    """Token index where the payoff beat begins.

    edge-tts splits on whitespace the same way, so counting tokens in the
    earlier beats locates the switch point in the word timeline without
    needing the model to report timings it does not have.
    """
    names = [name for name, _, _ in playbook.BEATS]
    upto = names.index(playbook.SWITCH_AT_BEAT)
    return sum(len(t.split()) for t in data["beat_texts"][:upto])


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

    layers: list = []

    # Two plates instead of one. The swap lands on the payoff beat, so the
    # frame changes at the moment the video starts delivering -- and the top
    # line changes with it, from the question to the answer.
    switch_idx = payoff_word_index(data)
    switch_at = words[switch_idx].start if 0 < switch_idx < len(words) else duration * 0.4
    switch_at = min(max(switch_at, 4.0), duration - 3.0) if duration > 8 else duration

    for variant, (top_line, start, end) in enumerate(
        [(data["hook"], 0.0, switch_at), (data["core"], switch_at, duration)]
    ):
        if end - start < 0.5:
            continue
        plate = workdir / f"bg{variant}.png"
        make_background(data["topic_label"], top_line, seed, font_path, variant).save(plate)
        img = ImageClip(str(plate))
        span_x, span_y = img.w - WIDTH, img.h - HEIGHT
        # Each plate drifts across its own half of the travel, so the motion
        # reads as one continuous move through the whole video.
        base = start / duration
        rate = (end - start) / duration
        layers.append(
            img.with_duration(end - start)
            .with_start(start)
            .with_position(
                lambda t, b=base, r=rate, sx=span_x, sy=span_y, d=(end - start): (
                    -sx * (b + r * (t / d)),
                    -sy * (1 - b - r * (t / d)),
                )
            )
        )

    # Opening card, held just long enough to read before the captions take
    # over mid-sentence.
    hook_path = workdir / "hook.png"
    make_hook_card(data["hook"], seed, font_path).save(hook_path)
    layers.append(
        ImageClip(str(hook_path)).with_duration(min(HOOK_SECONDS, duration)).with_start(0)
    )

    frames = render_caption_frames(
        words, font_path, seed, workdir / "caps", data.get("keywords", ())
    )
    print(f"[video] 자막 프레임 {len(frames)}개 / 전환 {switch_at:.1f}초")

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

    for path, start, end in make_progress_frames(duration, seed, workdir / "prog"):
        layers.append(
            ImageClip(str(path))
            .with_duration(end - start)
            .with_start(start)
            .with_position((0, SAFE_BOTTOM - 20))
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

    published = len(topics.load_used_ids())
    try:
        data = generate_script(client, topic, published)
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
