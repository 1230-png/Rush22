import asyncio
import csv
import datetime
import os
import json
import tempfile
import textwrap
from pathlib import Path

from google import genai
from google.genai import types
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from moviepy import ImageClip, AudioFileClip, CompositeVideoClip
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).resolve().parent
TOPIC_BANK = ROOT / "topic_bank.json"
USED_LOG = ROOT / "used_log.csv"

WIDTH, HEIGHT = 1080, 1920

# Tech/navy gradient, distinct per topic so videos aren't visually identical.
PALETTES = [
    ((18, 38, 68), (10, 58, 56)),    # navy -> teal
    ((28, 24, 58), (12, 46, 58)),    # indigo -> teal
    ((20, 30, 46), (46, 22, 58)),    # slate -> violet
]

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
]


def find_korean_font() -> str:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError("No Korean-capable font found; install fonts-noto-cjk")


def pick_palette(seed: str):
    idx = sum(ord(c) for c in seed) % len(PALETTES)
    return PALETTES[idx]


def draw_centered(draw, text, font, y, fill, wrap_width, canvas_width=WIDTH, line_gap=16):
    lines = textwrap.wrap(text, width=wrap_width) or [text]
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        draw.text(((canvas_width - w) / 2, y), line, font=font, fill=fill)
        y += h + line_gap
    return y


def make_background(title: str, topic_id: str, font_path: str) -> Image.Image:
    top_color, bottom_color = pick_palette(topic_id)
    img = Image.new("RGB", (WIDTH, HEIGHT), top_color)
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / HEIGHT
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b))

    label_font = ImageFont.truetype(font_path, 44)
    title_font = ImageFont.truetype(font_path, 66)
    brand_font = ImageFont.truetype(font_path, 34)

    y = 700
    y = draw_centered(draw, "오늘의 기술 트렌드", label_font, y, (150, 220, 210), wrap_width=20)
    y += 50
    draw_centered(draw, title, title_font, y, (255, 255, 255), wrap_width=13)

    brand = "Rush22 · 개발자 기술 트렌드"
    bbox = draw.textbbox((0, 0), brand, font=brand_font)
    bx = (WIDTH - (bbox[2] - bbox[0])) / 2
    draw.text((bx, HEIGHT - 140), brand, font=brand_font, fill=(255, 255, 255))
    return img


def load_used_ids() -> set:
    if not USED_LOG.exists():
        return set()
    with open(USED_LOG, newline="", encoding="utf-8") as f:
        return {row["topic_id"] for row in csv.DictReader(f)}


def pick_next_topic() -> dict:
    bank = json.loads(TOPIC_BANK.read_text(encoding="utf-8"))
    used = load_used_ids()
    for item in bank:
        if item["id"] not in used:
            return item
    # Full cycle done — start reusing from the top rather than stalling forever.
    return bank[0]


def append_log(row: dict) -> None:
    is_new = not USED_LOG.exists()
    with open(USED_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if is_new:
            writer.writeheader()
        writer.writerow(row)


async def generate_script(topic: str):
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    prompt = f"""
    주제: {topic}
    조건: 유튜브 쇼츠용으로 흥미롭고 간결하게 작성.
    반드시 아래 JSON 형식으로만 응답할 것 (마크다운 백틱 제외):
    {{
      "title": "영상 제목 (15자 내외, 화면에 표시될 짧은 제목)",
      "script": "음성 합성용 대본 텍스트",
      "tags": ["태그1", "태그2"]
    }}
    """
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
    )
    text = response.text.strip()
    if text.startswith("```json"):
        text = text[7:-3].strip()
    elif text.startswith("```"):
        text = text[3:-3].strip()
    return json.loads(text)


async def generate_tts(text: str, output_path: str):
    communicate = edge_tts.Communicate(text, "ko-KR-SunHiNeural")
    await communicate.save(output_path)


def create_video(image_path: str, audio_path: str, output_path: str):
    audio_clip = AudioFileClip(audio_path)
    duration = audio_clip.duration
    image_clip = ImageClip(image_path).with_duration(duration)
    video = CompositeVideoClip([image_clip]).with_audio(audio_clip)
    video.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")


def upload_to_youtube(video_path: str, title: str, tags: list) -> str:
    creds = Credentials(
        token=None,
        refresh_token=os.environ.get("YOUTUBE_REFRESH_TOKEN"),
        client_id=os.environ.get("YOUTUBE_CLIENT_ID"),
        client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": "Google AI & GitHub Actions 자동 생성 영상\n\n#개발자 #기술트렌드 #shorts",
            "tags": tags,
            "categoryId": "28"
        },
        "status": {
            "privacyStatus": "public"
        }
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
    video_id = response.get("id")
    print(f"업로드 완료! 영상 ID: {video_id}")
    return video_id


async def main():
    topic_item = pick_next_topic()
    font_path = find_korean_font()

    with tempfile.TemporaryDirectory() as tmpdir:
        bg_path = os.path.join(tmpdir, "background.png")
        audio_path = os.path.join(tmpdir, "voice.mp3")
        video_path = os.path.join(tmpdir, "final_output.mp4")

        print(f"1. 대본 생성 중... (주제: {topic_item['topic']})")
        data = await generate_script(topic_item["topic"])
        print(f"생성된 제목: {data['title']}")

        print("2. TTS 음성 합성 중...")
        await generate_tts(data["script"], audio_path)

        print("3. 배경 이미지 생성 중...")
        make_background(data["title"], topic_item["id"], font_path).save(bg_path)

        print("4. 영상 합성 중...")
        create_video(bg_path, audio_path, video_path)

        print("5. 유튜브 업로드 중...")
        video_id = upload_to_youtube(video_path, data["title"], data["tags"])

        append_log({
            "date": datetime.date.today().isoformat(),
            "topic_id": topic_item["id"],
            "topic": topic_item["topic"],
            "title": data["title"],
            "youtube_video_id": video_id,
        })


if __name__ == "__main__":
    asyncio.run(main())
