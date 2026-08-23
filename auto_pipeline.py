import asyncio
import os
import json
import tempfile
from google import genai
from google.genai import types
import edge_tts
from moviepy import ImageClip, AudioFileClip, TextClip, CompositeVideoClip
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

async def generate_script():
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    prompt = """
    주제: 개발자가 알아야 할 최신 기술 트렌드 1가지.
    조건: 유튜브 쇼츠용으로 흥미롭고 간결하게 작성.
    반드시 아래 JSON 형식으로만 응답할 것 (마크다운 백틱 제외):
    {
      "title": "영상 제목",
      "script": "음성 합성용 대본 텍스트",
      "tags": ["태그1", "태그2"]
    }
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
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
        font="Arial",
        font_size=50,
        color="white",
        bg_color="black",
        size=(image_clip.w, 100)
    ).with_duration(duration).with_position(("center", "bottom"))

    video = CompositeVideoClip([image_clip, txt_clip]).with_audio(audio_clip)
    video.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

def upload_to_youtube(video_path: str, title: str, tags: list):
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
    print(f"업로드 완료! 영상 ID: {response.get('id')}")

async def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "voice.mp3")
        video_path = os.path.join(tmpdir, "final_output.mp4")
        
        print("1. 대본 생성 중...")
        data = await generate_script()
        print(f"생성된 제목: {data['title']}")
        
        print("2. TTS 음성 합성 중...")
        await generate_tts(data["script"], audio_path)
        
        print("3. 영상 합성 중...")
        create_video("dummy.png", audio_path, video_path)
        
        print("4. 유튜브 업로드 중...")
        upload_to_youtube(video_path, data["title"], data["tags"])

if __name__ == "__main__":
    asyncio.run(main())
