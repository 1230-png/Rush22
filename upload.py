import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

def upload_video():
    client_id = os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
    refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN")

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )

    youtube = build("youtube", "v3", credentials=credentials)

    request_body = {
        "snippet": {
            "title": "자동 업로드 테스트 영상",
            "description": "GitHub Actions로 자동 업로드된 영상입니다.",
            "tags": ["automation", "test"],
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "private"  # 비공개 업로드
        }
    }

    media = MediaFileUpload("video.mp4", chunksize=-1, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=request_body,
        media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"업로드 진행률: {int(status.progress() * 100)}%")

    print(f"업로드 완료! 영상 ID: {response.get('id')}")

if __name__ == "__main__":
    upload_video()
