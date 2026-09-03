"""Visual rendering for Rush22 Shorts.

The old pipeline showed one still image for the whole video, which is the
single biggest reason a Short gets swiped away in the first two seconds.
This module builds a moving frame instead: a drifting gradient background
plus word-synced captions driven by the real timings edge-tts reports.
"""

from __future__ import annotations

import os
import random
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 1080, 1920

# The Shorts player draws its own chrome over the bottom ~400px (title,
# channel, action rail) and the top ~120px. Everything we care about has to
# live between these two lines or the player will sit on top of it.
SAFE_TOP = 150
SAFE_BOTTOM = HEIGHT - 400

# Vertical layout, all inside the safe band.
#   chip      190
#   hook      380   persistent, so a mid-scroll arrival knows the premise
#   captions  700
#   brand    1430
CHIP_TOP = 190
HOOK_TOP = 380
CAPTION_TOP = 700
CAPTION_HEIGHT = 520
BRAND_TOP = SAFE_BOTTOM - 90

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
]

# Each theme is a full look, not just a hue shift. Rotating them means two
# videos published the same day do not read as the same template with the
# text swapped -- which is exactly the "mass-produced" signal that gets a
# channel rejected at YPP review.
THEMES = [
    {
        "name": "midnight",
        "top": (14, 22, 46),
        "bottom": (8, 46, 58),
        "accent": (94, 234, 212),
        "chip": (34, 211, 190),
    },
    {
        "name": "ember",
        "top": (38, 16, 34),
        "bottom": (58, 24, 20),
        "accent": (251, 176, 96),
        "chip": (244, 143, 87),
    },
    {
        "name": "violet",
        "top": (26, 20, 54),
        "bottom": (16, 30, 62),
        "accent": (167, 139, 250),
        "chip": (139, 124, 246),
    },
    {
        "name": "forest",
        "top": (12, 34, 30),
        "bottom": (10, 24, 44),
        "accent": (134, 226, 148),
        "chip": (96, 200, 130),
    },
    {
        "name": "steel",
        "top": (22, 28, 38),
        "bottom": (40, 26, 48),
        "accent": (125, 196, 255),
        "chip": (96, 165, 250),
    },
]


def find_korean_font() -> str:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError("No Korean-capable font found; install fonts-noto-cjk")


def pick_theme(seed: str) -> dict:
    return THEMES[sum(ord(c) for c in seed) % len(THEMES)]


@dataclass
class Word:
    """One spoken word and when edge-tts said it lands."""

    text: str
    start: float
    end: float


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _gradient(theme: dict, height: int, width: int) -> Image.Image:
    """Vertical gradient. Drawn small and upscaled -- a 1080x2100 per-line
    loop in Python costs seconds per video for a result nobody can tell
    apart from the interpolated version."""
    small = Image.new("RGB", (2, 256))
    px = small.load()
    for y in range(256):
        t = y / 255
        color = (
            _lerp(theme["top"][0], theme["bottom"][0], t),
            _lerp(theme["top"][1], theme["bottom"][1], t),
            _lerp(theme["top"][2], theme["bottom"][2], t),
        )
        px[0, y] = color
        px[1, y] = color
    return small.resize((width, height), Image.BICUBIC)


def _add_texture(img: Image.Image, theme: dict, seed: int) -> None:
    """Soft glow blobs plus a faint code-ish grid.

    Both are cheap, and together they keep the background from reading as a
    flat CSS gradient once the Ken Burns drift is applied on top.
    """
    rng = random.Random(seed)
    w, h = img.size

    # Grid first, so the glow washes over it and the lines fade where the
    # light falls. Drawn on an alpha layer because a solid line stays at full
    # contrast no matter how bright the plate underneath gets -- that is what
    # turned an earlier pass into graph paper.
    grid = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grid)
    line = (*theme["accent"], 20)
    for x in range(0, w, 120):
        gdraw.line([(x, 0), (x, h)], fill=line, width=1)
    for y in range(0, h, 120):
        gdraw.line([(0, y), (w, y)], fill=line, width=1)
    img.paste(Image.alpha_composite(img.convert("RGBA"), grid).convert("RGB"))

    # Blobs at quarter scale, blurred, added back. Additive rather than
    # blended so the plate only ever lightens, but kept low: the captions
    # need a dark ground to stay legible after YouTube's re-encode.
    glow = Image.new("RGB", (w // 4, h // 4), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for _ in range(4):
        cx = rng.randint(0, w // 4)
        cy = rng.randint(0, h // 4)
        r = rng.randint(w // 14, w // 6)
        gd.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=tuple(int(c * 0.16) for c in theme["accent"]),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(38)).resize((w, h), Image.BICUBIC)
    img.paste(ImageChops.add(img, glow))


def make_background(
    topic_label: str,
    top_line: str,
    seed: str,
    font_path: str,
    variant: int = 0,
) -> Image.Image:
    """Background plate, rendered oversized so the video can drift across it.

    Carries everything that holds still for a stretch of the video: the topic
    chip, the top line, and the brand mark. Baking them into the plate means
    they drift with the background instead of sitting frozen on top of it,
    and it keeps the composite to one moving layer plus captions.

    `variant` produces the second plate. The video swaps to it when the
    payoff starts, so the frame visibly changes at the moment the content
    starts paying out rather than holding one image for forty seconds.
    """
    theme = pick_theme(seed)
    if variant:
        # Invert the gradient and swap accent roles: clearly a different
        # frame, still obviously the same video.
        theme = {
            **theme,
            "top": theme["bottom"],
            "bottom": theme["top"],
            "accent": theme["chip"],
            "chip": theme["accent"],
        }

    over_w, over_h = int(WIDTH * 1.14), int(HEIGHT * 1.14)

    img = _gradient(theme, over_h, over_w)
    _add_texture(img, theme, sum(ord(c) for c in seed) + variant * 31)

    draw = ImageDraw.Draw(img)
    off_x = (over_w - WIDTH) // 2
    off_y = (over_h - HEIGHT) // 2

    # Topic chip.
    chip_font = ImageFont.truetype(font_path, 40)
    label = topic_label if len(topic_label) <= 18 else topic_label[:17] + "…"
    bbox = draw.textbbox((0, 0), label, font=chip_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 38, 22
    cx = off_x + (WIDTH - (tw + pad_x * 2)) / 2
    cy = off_y + CHIP_TOP
    draw.rounded_rectangle(
        [cx, cy, cx + tw + pad_x * 2, cy + th + pad_y * 2],
        radius=(th + pad_y * 2) // 2,
        fill=theme["chip"],
    )
    draw.text((cx + pad_x, cy + pad_y - bbox[1]), label, font=chip_font, fill=(10, 16, 24))

    # Hook line, held for the whole video. Shorts viewers arrive mid-scroll
    # and mid-sentence; without this they have no idea what the question is.
    hook_font = ImageFont.truetype(font_path, 62)
    hook_lines = textwrap.wrap(top_line, width=14)[:2] or [top_line]
    hy = off_y + HOOK_TOP
    for line in hook_lines:
        bb = draw.textbbox((0, 0), line, font=hook_font)
        hx = off_x + (WIDTH - (bb[2] - bb[0])) / 2
        draw.text((hx + 3, hy + 4), line, font=hook_font, fill=(0, 0, 0))
        draw.text((hx, hy), line, font=hook_font, fill=(236, 242, 250))
        hy += hook_font.getbbox("가")[3] + 22

    # Accent rule between the hook and the caption band.
    rule_y = off_y + HOOK_TOP + 200
    draw.rounded_rectangle(
        [off_x + (WIDTH - 120) / 2, rule_y, off_x + (WIDTH + 120) / 2, rule_y + 8],
        radius=4,
        fill=theme["accent"],
    )

    # No brand mark: the Shorts player already shows the channel name and
    # handle, and a mark baked into the drifting plate collided with the
    # fixed progress bar once the plate moved.
    return img


def make_hook_card(hook: str, seed: str, font_path: str) -> Image.Image:
    """Full-frame opener.

    Shorts live or die in the first second, so the video opens on the hook
    line alone at large size rather than easing in with narration over an
    empty frame.
    """
    theme = pick_theme(seed)
    img = _gradient(theme, HEIGHT, WIDTH)
    _add_texture(img, theme, sum(ord(c) for c in seed) + 7)
    draw = ImageDraw.Draw(img)

    font = ImageFont.truetype(font_path, 96)
    lines = textwrap.wrap(hook, width=9) or [hook]
    line_h = font.getbbox("가")[3] + 30
    y = (HEIGHT - len(lines) * line_h) / 2 - 60
    for line in lines:
        bb = draw.textbbox((0, 0), line, font=font)
        x = (WIDTH - (bb[2] - bb[0])) / 2
        draw.text((x + 4, y + 4), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_h

    bar_w = 220
    draw.rounded_rectangle(
        [(WIDTH - bar_w) / 2, y + 40, (WIDTH + bar_w) / 2, y + 52],
        radius=6,
        fill=theme["accent"],
    )
    return img


def group_words(words: list[Word], max_chars: int = 18) -> list[list[Word]]:
    """Chunk words into caption-sized phrases.

    A phrase also breaks on a long pause, so the captions follow the
    narration's own phrasing instead of cutting mid-thought at an arbitrary
    character count.
    """
    groups: list[list[Word]] = []
    current: list[Word] = []
    length = 0
    for w in words:
        gap = w.start - current[-1].end if current else 0
        if current and (length + len(w.text) > max_chars or gap > 0.45):
            groups.append(current)
            current, length = [], 0
        current.append(w)
        length += len(w.text) + 1
    if current:
        groups.append(current)
    return groups


def keyword_parts(keywords: tuple[str, ...]) -> tuple[str, ...]:
    """Flatten keywords into the individual terms captions can match.

    A keyword like "참조 카운트" is two spoken tokens, so testing the whole
    phrase against one token never matches -- which silently left every
    multi-word keyword unhighlighted. Parts shorter than two characters are
    dropped: they hit far too much.
    """
    parts = {p for k in keywords for p in k.split() if len(p) >= 2}
    return tuple(parts)


def is_keyword(token: str, parts: tuple[str, ...]) -> bool:
    """Whether a spoken token carries one of the video's key terms.

    Substring rather than equality: Korean attaches particles to nouns, so
    "GIL" arrives as "GIL이" and "캐시" as "캐시를". Matching on equality
    would leave almost every keyword unhighlighted.
    """
    return any(p in token for p in parts)


def render_caption(
    phrase: list[Word],
    spoken_upto: int,
    font_path: str,
    theme: dict,
    keywords: tuple[str, ...] = (),
) -> Image.Image:
    """One caption frame: the phrase, with words already spoken lit up.

    Returned as an RGBA band rather than a full frame. Compositing a
    1080x520 strip per word is what keeps a 45-second render at ~120 words
    from turning into a multi-minute encode.
    """
    parts = keyword_parts(keywords)
    img = Image.new("RGBA", (WIDTH, CAPTION_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 74)
    space = draw.textlength(" ", font=font)

    # Wrap by measured width so mixed Korean/Latin lines break correctly --
    # character counts lie when "WebAssembly" sits next to Hangul.
    lines: list[list[tuple[Word, int]]] = [[]]
    widths: list[float] = [0.0]
    for idx, w in enumerate(phrase):
        tw = draw.textlength(w.text, font=font)
        extra = tw + (space if lines[-1] else 0)
        if lines[-1] and widths[-1] + extra > WIDTH - 130:
            lines.append([])
            widths.append(0.0)
            extra = tw
        lines[-1].append((w, idx))
        widths[-1] += extra

    line_h = font.getbbox("가")[3] + 26
    y = (CAPTION_HEIGHT - len(lines) * line_h) / 2

    for line, total in zip(lines, widths):
        x = (WIDTH - total) / 2
        for w, idx in line:
            spoken = idx <= spoken_upto
            if idx == spoken_upto:
                fill = theme["accent"]
            elif spoken and is_keyword(w.text, parts):
                # Key terms stay lit after they are spoken. A viewer who
                # joins mid-sentence can still see what the video is about.
                fill = theme["chip"]
            elif spoken:
                fill = (255, 255, 255)
            else:
                fill = (168, 178, 196)
            # Hard shadow instead of a blur: it survives YouTube's
            # re-encode, where a soft glow turns to mush.
            draw.text((x + 4, y + 5), w.text, font=font, fill=(0, 0, 0, 210))
            draw.text((x, y), w.text, font=font, fill=fill)
            x += draw.textlength(w.text, font=font) + space
        y += line_h

    return img


def render_caption_frames(
    words: list[Word],
    font_path: str,
    seed: str,
    out_dir: Path,
    keywords: tuple[str, ...] = (),
) -> list[tuple[Path, float, float]]:
    """Render every caption state to disk.

    Returns (path, start, end) per word so the caller can lay the strips out
    on the timeline without re-deriving the timings.
    """
    theme = pick_theme(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[Path, float, float]] = []

    for g_idx, phrase in enumerate(group_words(words)):
        for w_idx, word in enumerate(phrase):
            path = out_dir / f"cap_{g_idx:03d}_{w_idx:02d}.png"
            render_caption(phrase, w_idx, font_path, theme, keywords).save(path)
            # Hold the last word of a phrase until the next one starts so the
            # caption never blinks out into an empty band between phrases.
            end = phrase[w_idx + 1].start if w_idx + 1 < len(phrase) else word.end
            frames.append((path, word.start, max(end, word.start + 0.08)))

    return frames


def make_progress_frames(
    duration: float,
    seed: str,
    out_dir: Path,
    steps: int = 48,
) -> list[tuple[Path, float, float]]:
    """A thin bar that fills as the video plays.

    Shorts viewers cannot see the scrubber, so there is nothing telling them
    how much is left; a visible finish line is one of the cheapest ways to
    hold someone through the payoff. Pre-rendered in steps rather than
    animated per frame -- at 48 steps over ~45s the growth reads as
    continuous and costs 48 tiny PNGs instead of a per-frame resize.
    """
    theme = pick_theme(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[Path, float, float]] = []
    span = duration / steps
    bar_h = 10
    margin = 90

    for i in range(steps):
        img = Image.new("RGBA", (WIDTH, bar_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [margin, 0, WIDTH - margin, bar_h], radius=bar_h // 2, fill=(255, 255, 255, 40)
        )
        filled = margin + (WIDTH - margin * 2) * (i + 1) / steps
        draw.rounded_rectangle(
            [margin, 0, filled, bar_h], radius=bar_h // 2, fill=(*theme["accent"], 235)
        )
        path = out_dir / f"prog_{i:03d}.png"
        img.save(path)
        frames.append((path, i * span, (i + 1) * span))

    return frames
