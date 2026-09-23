"""Korean Daily Ears: write a Korean-learning script, render it, upload it to YouTube.

usage: python main.py short|long
env:   GEMINI_API_KEY, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
       (without YT_* the video is rendered to out/ but not uploaded)
"""
import asyncio
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

import edge_tts
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"
HISTORY = ROOT / "history.json"
FONT = os.environ.get("FONT", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
# tried in order; the free tier often returns 503 on one model while another works
MODELS = os.environ.get("GEMINI_MODELS", "gemini-3.6-flash,gemini-flash-latest,gemini-2.5-flash").split(",")
KO_VOICES = ["ko-KR-SunHiNeural", "ko-KR-InJoonNeural"]
EN_VOICE = "en-US-AriaNeural"
SIZE = {"short": (1080, 1920), "long": (1920, 1080)}
COUNT = {"short": 6, "long": 120}
BG = ["#1b1f3b", "#2d1b3b", "#10302b", "#3b2a1b", "#1b2f3b"]

# (format, prompt description, playlist title) - rotated in order per kind
FORMATS = {
    "short": [
        ("slang", "Korean slang and trending words young Koreans actually use in {year}", "Korean Slang"),
        ("real_vs_textbook", "Textbook Korean vs what Koreans really say (put the textbook version in note)", "Textbook vs Real Korean"),
        ("situation", "one real-life situation in Korea (e.g. cafe, convenience store, taxi, subway, hospital, "
                      "K-pop concert, hair salon, delivery app, dating, office) as a short two-person dialogue", "Korean for Real-Life Situations"),
        ("season", "Korean phrases for what is happening in Korea around {date} (holidays, season, events)", "Seasonal Korean"),
    ],
    "long": [
        ("sleep", "Learn Korean while sleeping: calm everyday phrases on one theme", "Learn Korean While Sleeping"),
        ("topik", "TOPIK vocabulary for one level (1-6): each item is a useful example sentence using the word", "TOPIK Vocabulary"),
        ("conversation", "Korean conversation practice: several two-person dialogues on one theme", "Korean Conversation Practice"),
    ],
}
DIALOGUE = {"situation", "conversation"}
FOOTER = "Subscribe for daily Korean listening practice: https://www.youtube.com/@KoreanDailyEars\n\n#LearnKorean #KoreanPhrases #KoreanListening"


def retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"retry {i + 1}: {e}", file=sys.stderr)
            time.sleep(60 * (i + 1))


def gemini(prompt):
    err = None
    for model in MODELS:
        try:
            return gemini_model(model, prompt)
        except Exception as e:
            print(f"{model}: {e}", file=sys.stderr)
            err = e
    raise err


def gemini_model(model, prompt):
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"])


def write_script(kind, desc, hist):
    today = datetime.date.today()
    prompt = f"""You write scripts for "Korean Daily Ears", a YouTube channel teaching Korean to English speakers.
Video type: {desc.format(year=today.year, date=today.isoformat())}
Make exactly {COUNT[kind]} items. Pick a fresh, specific theme people search for.
Do not repeat these recent video titles: {json.dumps([v["title"] for v in hist["videos"][-30:]], ensure_ascii=False)}
Do not reuse these phrases: {json.dumps(hist["phrases"][-300:], ensure_ascii=False)}
Return JSON only:
{{"title": "catchy English YouTube title with searchable keywords like 'Learn Korean', max 90 chars",
  "description": "2-3 English sentences about the video",
  "tags": ["10 English search tags"],
  "items": [{{"ko": "natural Korean, max 40 chars", "roman": "revised romanization",
              "en": "natural English meaning", "note": "short English tip, max 60 chars, or empty"}}]}}
Rules: modern Korean as native speakers in Seoul really say it; correct spelling and spacing;
no song lyrics, drama quotes, real people or brand names."""
    script = retry(lambda: gemini(prompt))
    review = ("You are a strict native Korean editor and teacher. Fix any unnatural Korean, spelling or spacing "
              "errors, wrong romanization and wrong English translations in this JSON. Keep the same structure "
              "and item count. Return the corrected JSON only.\n" + json.dumps(script, ensure_ascii=False))
    return retry(lambda: gemini(review))


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *map(str, args)], check=True)


def font(size):
    return ImageFont.truetype(FONT, size)


def wrap(draw, text, f, width):
    lines, line = [], ""
    for word in text.split():
        test = f"{line} {word}".strip()
        if not line or draw.textlength(test, font=f) <= width:
            line = test
        else:
            lines.append(line)
            line = word
    return lines + [line] if line else lines


def card(path, size, bg, rows, footer=""):
    """rows: [(text, font size, color)], drawn as one vertically centered block."""
    w, h = size
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    lines = []
    for text, fs, color in rows:
        if not text:
            continue
        f = font(fs)
        lines += [(ln, f, color, int(fs * 1.3)) for ln in wrap(d, text, f, w * 0.85)]
        lines[-1] = (*lines[-1][:3], int(fs * 1.9))  # gap after each row
    y = (h - sum(step for *_, step in lines)) // 2
    for ln, f, color, step in lines:
        d.text((w / 2, y), ln, font=f, fill=color, anchor="mt")
        y += step
    d.text((w / 2, h * 0.05), "Korean Daily Ears", font=font(36), fill="#8899aa", anchor="mt")
    if footer:
        d.text((w / 2, h * 0.93), footer, font=font(36), fill="#8899aa", anchor="mt")
    img.save(path)


def tts(text, voice, path, rate="+0%"):
    retry(lambda: asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(path))))


def join_audio(parts, out):
    """parts: mp3 paths or float seconds of silence -> one stereo 48k aac file."""
    args = []
    for p in parts:
        args += ["-f", "lavfi", "-t", p, "-i", "anullsrc=r=24000:cl=mono"] if isinstance(p, float) else ["-i", p]
    ff(*args, "-filter_complex", "".join(f"[{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1",
       "-ar", 48000, "-ac", 2, "-c:a", "aac", out)


def segment(png, m4a, out):
    ff("-loop", 1, "-framerate", 30, "-i", png, "-i", m4a, "-c:v", "libx264", "-preset", "veryfast",
       "-tune", "stillimage", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", out)


def render(kind, fmt, script, bg):
    OUT.mkdir(exist_ok=True)
    size, items = SIZE[kind], script["items"]
    card(OUT / "intro.png", size, bg, [(script["title"], 72, "white")])
    join_audio([1.5], OUT / "intro.m4a")
    segment(OUT / "intro.png", OUT / "intro.m4a", OUT / "seg_intro.mp4")
    segs = [OUT / "seg_intro.mp4"]
    for i, it in enumerate(items):
        ko_voice = KO_VOICES[i % 2] if fmt in DIALOGUE else KO_VOICES[0]
        tts(it["ko"], ko_voice, OUT / "ko.mp3")
        tts(it["en"], EN_VOICE, OUT / "en.mp3")
        tts(it["ko"], ko_voice, OUT / "slow.mp3", rate="-25%")
        parts = [OUT / "ko.mp3", 0.6, OUT / "en.mp3", 0.6, OUT / "slow.mp3", 1.0]
        if kind == "long":  # one more repetition for listening practice
            parts += [OUT / "ko.mp3", 1.2]
        join_audio(parts, OUT / "item.m4a")
        card(OUT / "item.png", size, bg, [(it["ko"], 96, "white"), (it["roman"], 44, "#9fb4c7"),
                                          (it["en"], 58, "#ffd166"), (it.get("note") or "", 38, "#c0c0c0")],
             footer=f"{i + 1} / {len(items)}")
        seg = OUT / f"seg_{i:03d}.mp4"
        segment(OUT / "item.png", OUT / "item.m4a", seg)
        segs.append(seg)
    (OUT / "list.txt").write_text("".join(f"file '{s.name}'\n" for s in segs), encoding="utf-8")
    ff("-f", "concat", "-safe", 0, "-i", OUT / "list.txt", "-c", "copy", OUT / "video.mp4")
    return OUT / "video.mp4"


def youtube():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"], client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"], token_uri="https://oauth2.googleapis.com/token")
    return build("youtube", "v3", credentials=creds)


def upload(yt, path, kind, script, items):
    from googleapiclient.http import MediaFileUpload
    title = script["title"][:90] + (" #shorts" if kind == "short" else "")
    desc = "\n".join([script["description"], "", *(f"{it['ko']} - {it['en']}" for it in items[:40]), "", FOOTER])
    tags, total = [], 0
    for t in script.get("tags", []):  # YouTube caps tags at 500 chars total
        if total + len(t) > 450:
            break
        tags.append(t)
        total += len(t) + 1
    body = {"snippet": {"title": title, "description": desc[:4900], "tags": tags, "categoryId": "27",
                        "defaultLanguage": "en", "defaultAudioLanguage": "ko"},
            "status": {"privacyStatus": os.environ.get("PRIVACY", "public"), "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def add_to_playlist(yt, hist, fmt, playlist_title, video_id):
    pid = hist["playlists"].get(fmt)
    if not pid:
        pid = yt.playlists().insert(part="snippet,status", body={
            "snippet": {"title": playlist_title, "defaultLanguage": "en"},
            "status": {"privacyStatus": "public"}}).execute()["id"]
        hist["playlists"][fmt] = pid
    yt.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()


def main(kind):
    hist = json.loads(HISTORY.read_text(encoding="utf-8"))
    n = sum(v["kind"] == kind for v in hist["videos"])
    fmt, desc, playlist_title = FORMATS[kind][n % len(FORMATS[kind])]
    script = write_script(kind, desc, hist)
    seen = set(hist["phrases"])
    items = [it for it in script["items"] if it["ko"] not in seen] or script["items"]
    script["items"] = items
    print(f"{kind}/{fmt}: {script['title']} ({len(items)} items)")
    video = render(kind, fmt, script, BG[len(hist["videos"]) % len(BG)])
    if not os.environ.get("YT_REFRESH_TOKEN"):
        print(f"rendered {video}; no YT_REFRESH_TOKEN, skipping upload")
        return
    yt = youtube()
    video_id = upload(yt, video, kind, script, items)
    try:  # a playlist failure must not lose the history of an already-uploaded video
        add_to_playlist(yt, hist, fmt, playlist_title, video_id)
    except Exception as e:
        print(f"playlist skipped: {e}", file=sys.stderr)
    hist["videos"].append({"date": datetime.date.today().isoformat(), "kind": kind, "format": fmt,
                           "title": script["title"], "id": video_id})
    hist["phrases"] += [it["ko"] for it in items]
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"uploaded https://youtu.be/{video_id}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "short")
