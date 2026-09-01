import asyncio
import csv
import datetime
import os
import json
import tempfile
from pathlib import Path

from google import genai
from google.genai import types
import edge_tts
from moviepy import ImageClip, AudioFileClip, TextClip, CompositeVideoClip
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).resolve().parent
TOPIC_BANK = ROOT / "topic_bank.json"
USED_LOG = ROOT / "used_log.csv"


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
      "title": "영상 제목",
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

    txt_clip = TextClip(
        text="자동 생성된 유튜브 쇼츠",
        font="/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        font_size=50,
        color="white",
        bg_color="black",
        size=(image_clip.w, 100)
    ).with_duration(duration).with_position(("center", "bottom"))

    video = CompositeVideoClip([image_clip, txt_clip]).with_audio(audio_clip)
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
            "description": "Google AI & GitHub Actions 자동 생성 영상",
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

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "voice.mp3")
        video_path = os.path.join(tmpdir, "final_output.mp4")

        print(f"1. 대본 생성 중... (주제: {topic_item['topic']})")
        data = await generate_script(topic_item["topic"])
        print(f"생성된 제목: {data['title']}")

        print("2. TTS 음성 합성 중...")
        await generate_tts(data["script"], audio_path)

        print("3. 영상 합성 중...")
        create_video("dummy.png", audio_path, video_path)

        print("4. 유튜브 업로드 중...")
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
