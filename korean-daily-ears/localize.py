"""Push channel_translations.json into the channel's localized descriptions.

python localize.py [--dry-run]

The localized title stays exactly the channel name. A first attempt used names like
"Korean Daily Ears | Aprende Coreano" and YouTube rejected them ("too many unavailable
names"), so this refuses to run unless the live channel title matches the file.
"""
import json
import pathlib
import sys

from main import youtube

data = json.loads((pathlib.Path(__file__).parent / "channel_translations.json").read_text(encoding="utf-8"))
dry = "--dry-run" in sys.argv

yt = youtube()
ch = yt.channels().list(part="snippet,brandingSettings,localizations", mine=True).execute()["items"][0]
title = ch["snippet"]["title"]
if title != data["name"]:
    sys.exit(f"channel title is {title!r}, expected {data['name']!r}; not touching it")

loc = ch.get("localizations", {})
for lang, desc in data["descriptions"].items():
    loc[lang] = {"title": data["name"], "description": desc}
print(f"{title}: {len(data['descriptions'])} languages -> {', '.join(data['descriptions'])}")

# YouTube only accepts localizations once the channel has a default language.
branding = ch["brandingSettings"]
if not branding.get("channel", {}).get("defaultLanguage"):
    print("default language not set; setting it to en")
    if not dry:
        branding.setdefault("channel", {})["defaultLanguage"] = "en"
        yt.channels().update(part="brandingSettings", body={"id": ch["id"], "brandingSettings": branding}).execute()

if dry:
    print("dry run: nothing sent")
else:
    yt.channels().update(part="localizations", body={"id": ch["id"], "localizations": loc}).execute()
    got = yt.channels().list(part="localizations", id=ch["id"]).execute()["items"][0].get("localizations", {})
    missing = [lang for lang in data["descriptions"] if lang not in got]
    print(f"saved {len(got)} localizations" + (f"; missing {missing}" if missing else ""))
    if missing:
        sys.exit(1)
