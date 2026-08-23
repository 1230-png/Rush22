# auto_pipeline.py
import asyncio
import json
import os
from google import genai
from google.genai import types
import edge_tts
from moviepy.editor import ImageClip, AudioFileClip, TextClip, CompositeVideoClip
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

async def generate_script(client: genai.Client) -> dict:
    prompt = """
    주제: 최신 기술 트렌드에 대한 1분 쇼츠 대본
    조건: JSON 형식으로 반환할 것. 키는 'title', 'description', 'scenes' (각 씬은 'text'와 'image_prompt' 포함)
    """
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        ),
    )
    return json.loads(response.text)

async def generate_tts(text: str, output_path: str):
    communicate = edge_tts.Communicate(text, "ko-KR-SunHiNeural")
    await communicate.save(output_path)

def create_video_clip(image_path: str, audio_path: str, text: str, output_path: str):
    audio = AudioFileClip(audio_path)
    duration = audio.duration

    image_clip = ImageClip(image_path).set_duration(duration)
    
    def zoom_in(get_frame, t):
        img = get_frame(t)
        return img

    resized_clip = image_clip.fl(zoom_in, apply_to=['mask'])
    
    txt_clip = TextClip(text, fontsize=24, color='white', bg_color='black', size=(720, 100))
    txt_clip = txt_clip.set_duration(duration).set_pos(('center', 'bottom'))

    video = CompositeVideoClip([resized_clip, txt_clip]).set_audio(audio)
    video.write_videofile(output_path, fps=24, codec='libx264', audio_codec='aac')

def upload_youtube(video_path: str, title: str, description: str):
    credentials = Credentials(
        token=None,
        refresh_token=os.environ.get("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ.get("YOUTUBE_CLIENT_ID"),
        client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    youtube = build("youtube", "v3", credentials=credentials)

    request_body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "private"
        }
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=request_body, media_body=media)
    
    response = None
    while response is None:
        status, response = request.next_chunk()

async def main():
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    script_data = await generate_script(client)
    
    await generate_tts(script_data['scenes'][0]['text'], "audio.mp3")
    create_video_clip("dummy.png", "audio.mp3", script_data['scenes'][0]['text'], "final_output.mp4")
    
    upload_youtube("final_output.mp4", script_data['title'], script_data['description'])

if __name__ == "__main__":
    asyncio.run(main())
