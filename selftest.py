"""Offline render check.

Exercises everything except Gemini and the YouTube upload, so a change to
the renderer can be verified before it ships to a schedule nobody is
watching. Run: python selftest.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from render import CAPTION_TOP, HEIGHT, SAFE_BOTTOM, WIDTH, Word, find_korean_font

SAMPLE = {
    "hook": "GIL이 발목을 잡는다",
    "title": "파이썬 GIL이 멀티스레딩을 막는 진짜 이유",
    "topic_label": "파이썬 내부구조",
    "summary": "GIL은 스레드를 막는 게 아니라 인터프리터를 지킵니다.",
    "script": (
        "파이썬에서 스레드를 여덟 개 띄웠는데 왜 속도가 그대로일까요? "
        "범인은 GIL, 글로벌 인터프리터 락입니다. "
        "파이썬 객체는 참조 카운트로 메모리를 관리하는데, "
        "여러 스레드가 이 숫자를 동시에 건드리면 값이 깨집니다. "
        "그래서 인터프리터는 한 번에 한 스레드만 바이트코드를 실행하게 막아둡니다. "
        "다만 파일을 읽거나 네트워크를 기다리는 동안에는 락이 풀립니다. "
        "그래서 입출력 작업은 스레드로 빨라지고, 계산 작업은 전혀 빨라지지 않습니다. "
        "느리다고 스레드를 늘리기 전에, 그 작업이 CPU를 쓰는지 기다리는지부터 보세요."
    ),
    "tags": ["파이썬", "GIL", "멀티스레딩", "백엔드"],
}


def synthetic_words(text: str) -> list[Word]:
    """Stand-in timings for when edge-tts is unreachable.

    Roughly matches Korean narration pace so the caption layout is exercised
    at a realistic word count and duration.
    """
    tokens = text.split()
    words, t = [], 0.0
    for tok in tokens:
        dur = 0.16 + len(tok) * 0.075
        words.append(Word(tok, t, t + dur))
        t += dur + 0.045
    return words


async def get_words(text: str, out: Path) -> tuple[list[Word], bool]:
    import edge_tts

    from auto_pipeline import VOICE, VOICE_RATE

    try:
        communicate = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE)
        audio = bytearray()
        words: list[Word] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 10_000_000
                words.append(Word(chunk["text"], start, start + chunk["duration"] / 10_000_000))
        if audio and words:
            out.write_bytes(bytes(audio))
            return words, True
        raise RuntimeError("빈 응답")
    except Exception as exc:  # noqa: BLE001
        print(f"  edge-tts 사용 불가 ({type(exc).__name__}) — 합성 타이밍으로 대체")
        return synthetic_words(text), False


def make_silence(path: Path, seconds: float) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"anullsrc=r=24000:cl=mono:d={seconds:.2f}",
            "-c:a", "libmp3lame", str(path),
        ],
        check=True,
        capture_output=True,
    )


async def main() -> int:
    from auto_pipeline import build_video

    out_dir = Path("selftest_out")
    out_dir.mkdir(exist_ok=True)
    audio = out_dir / "voice.mp3"
    video = out_dir / "short.mp4"

    font = find_korean_font()
    print(f"폰트: {font}")

    print("내레이션 준비...")
    words, real = await get_words(SAMPLE["script"], audio)
    if not real:
        make_silence(audio, words[-1].end + 0.6)
    print(f"  단어 {len(words)}개 / {words[-1].end:.1f}초 (real_tts={real})")

    print("렌더링...")
    t0 = time.time()
    duration = build_video(audio, words, SAMPLE, "T001", font, out_dir, video)
    elapsed = time.time() - t0

    size_mb = video.stat().st_size / 1_048_576
    print(f"  {duration:.1f}초 영상 / {size_mb:.1f}MB / 렌더 {elapsed:.0f}초")

    problems = []
    if not (20 <= duration <= 58):
        problems.append(f"길이 이탈: {duration:.1f}s")
    if CAPTION_TOP + 520 > SAFE_BOTTOM + 400:
        problems.append("자막이 안전 영역을 벗어남")
    if size_mb > 90:
        problems.append(f"파일 과대: {size_mb:.1f}MB")
    if elapsed > 600:
        problems.append(f"렌더 과다: {elapsed:.0f}s")

    # A still frame from the middle proves captions actually composited --
    # a black or caption-less frame is the failure this catches.
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{duration/2:.1f}", "-i", str(video),
         "-frames:v", "1", str(out_dir / "frame_mid.png")],
        check=True, capture_output=True,
    )
    print(f"  중간 프레임: {out_dir / 'frame_mid.png'}")

    if problems:
        print("\n실패:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\n통과")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
