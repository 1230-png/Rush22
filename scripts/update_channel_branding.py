"""One-off / re-runnable: upload the channel banner and set channel keywords.

Run via the "Update channel branding" workflow (workflow_dispatch). Re-run
any time the banner image or keyword list changes.
"""
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent.parent
BANNER_PATH = ROOT / "assets" / "banner.png"

KEYWORDS = [
    "개발자", "프로그래밍", "기술 트렌드", "AI", "코딩",
    "software development", "tech trends", "programming", "developer",
]


def get_client():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("youtube", "v3", credentials=creds)


def main():
    youtube = get_client()

    print("배너 업로드 중...")
    banner_resp = youtube.channelBanners().insert(
        media_body=MediaFileUpload(str(BANNER_PATH), mimetype="image/png")
    ).execute()
    banner_url = banner_resp["url"]
    print("배너 업로드 완료:", banner_url)

    channel = youtube.channels().list(part="brandingSettings,id", mine=True).execute()
    item = channel["items"][0]
    channel_id = item["id"]
    branding = item.get("brandingSettings", {})
    branding.setdefault("channel", {})
    branding.setdefault("image", {})

    branding["channel"]["keywords"] = " ".join(KEYWORDS)
    branding["image"]["bannerExternalUrl"] = banner_url

    youtube.channels().update(
        part="brandingSettings",
        body={"id": channel_id, "brandingSettings": branding},
    ).execute()
    print("채널 키워드/배너 반영 완료")


if __name__ == "__main__":
    main()
