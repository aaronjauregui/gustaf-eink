#!/usr/bin/env python3
"""Render a merged weekly calendar as a PNG for an e-ink display."""
from __future__ import annotations

import io
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo

import boto3
import requests
from icalendar import Calendar
import recurring_ical_events
from PIL import Image, ImageDraw, ImageFont

# ---------- config ----------
TZ = ZoneInfo("Australia/Sydney")
DAYS = 6
WIDTH, HEIGHT = 800, 480          # match your display
OUTPUT = "/tmp/calendar.png"
FONT_REG = "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"
FONT_SERIF_ITALIC = "/usr/share/fonts/truetype/noto/NotoSerif-Italic.ttf"
FONT_SYMBOLS = "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf"
FONT_MONO = "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf"
_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_GENTIUM = os.path.join(_HERE, "fonts/GentiumPlus-Regular.ttf")
FONT_GENTIUM_BOLD = os.path.join(_HERE, "fonts/GentiumPlus-Bold.ttf")
FONT_GENTIUM_ITALIC = os.path.join(_HERE, "fonts/GentiumPlus-Italic.ttf")

# Weather (Open-Meteo, no API key). Defaults: Canberra.
WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "-35.2809"))
WEATHER_LON = float(os.environ.get("WEATHER_LON", "149.1300"))

# Cloudflare R2 upload target. Leave R2_BUCKET empty to skip uploading.
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_KEY = os.environ.get("R2_KEY", "calendar.png")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")


@dataclass
class Source:
    name: str
    url: str
    auth: tuple[str, str] | None = None  # (user, pass) for basic auth


# Read-only Nextcloud account: the calendars are shared with it by admin (the
# "_shared_by_admin" URIs), holidays is its own subscription. Password is an
# app password, revocable on its own.
NC_USER = os.environ.get("NC_USER", "eink-cal")
NC_CALENDARS = f"https://gustaf.kinoko.house/remote.php/dav/calendars/{NC_USER}"
NC_AUTH = (NC_USER, os.environ.get("NC_PASS", ""))

SOURCES = [
    Source("Holidays in Australia", f"{NC_CALENDARS}/holidays-in-australia/?export", auth=NC_AUTH),
    Source("Meal Plan", f"{NC_CALENDARS}/chefcal_shared_by_admin/?export", auth=NC_AUTH),
    Source("Gustaf", f"{NC_CALENDARS}/personal_shared_by_admin/?export", auth=NC_AUTH),
    # With auth
    #Source("Chef",   "https://nextcloud.lan/remote.php/dav/calendars/aaron/chef/?export",
    #       auth=("admin", os.environ.get("NC_PASS", ""))),
    # No auth
    #Source("Shared", "https://example.com/shared.ics"),
]
# ---------------------------


@dataclass
class Event:
    start: datetime
    end: datetime
    summary: str
    source: str
    all_day: bool


@dataclass
class DailyWeather:
    code: int
    tmin: float
    tmax: float


# Last good forecast, used when Open-Meteo is unreachable. Set WEATHER_CACHE=""
# to disable (e.g. in a stateless container, where the file can't persist).
WEATHER_CACHE = os.environ.get("WEATHER_CACHE", os.path.join(_HERE, "weather_cache.json"))


def _load_weather_cache() -> tuple[date | None, dict[date, DailyWeather]]:
    if not WEATHER_CACHE:
        return None, {}
    try:
        with open(WEATHER_CACHE) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None, {}
    fetched_on = date.fromisoformat(raw["fetched_on"]) if raw.get("fetched_on") else None
    entries = {
        date.fromisoformat(k): DailyWeather(int(v["code"]), float(v["tmin"]), float(v["tmax"]))
        for k, v in raw.get("entries", {}).items()
    }
    return fetched_on, entries


def _save_weather_cache(fetched_on: date, cache: dict[date, DailyWeather]) -> None:
    if not WEATHER_CACHE:
        return
    raw = {
        "fetched_on": fetched_on.isoformat(),
        "entries": {k.isoformat(): {"code": v.code, "tmin": v.tmin, "tmax": v.tmax}
                    for k, v in cache.items()},
    }
    tmp = WEATHER_CACHE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(raw, f)
        os.replace(tmp, WEATHER_CACHE)
    except OSError as e:
        print(f"warn: weather cache write: {e}", file=sys.stderr)


def fetch_weather(start_day: date, days: int) -> dict[date, DailyWeather]:
    end_day = start_day + timedelta(days=days - 1)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min"
        f"&timezone={TZ.key.replace('/', '%2F')}"
        f"&start_date={start_day:%Y-%m-%d}&end_date={end_day:%Y-%m-%d}"
    )
    today = datetime.now(TZ).date()
    fetched_on, cache = _load_weather_cache()
    if fetched_on != today:
        cache = {}
    fresh: dict[date, DailyWeather] = {}
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        j = r.json()["daily"]
        for ds, code, tmax, tmin in zip(
            j["time"], j["weather_code"],
            j["temperature_2m_max"], j["temperature_2m_min"],
        ):
            fresh[date.fromisoformat(ds)] = DailyWeather(int(code), float(tmin), float(tmax))
    except Exception as e:
        print(f"warn: weather: {e}", file=sys.stderr)
    if fresh:
        cache.update(fresh)
        _save_weather_cache(today, cache)
    return {d: cache[d] for d in (start_day + timedelta(days=i) for i in range(days)) if d in cache}


WEATHER_GLYPHS = {
    "sun": "☀",     # ☀
    "partly": "⛅",  # ⛅
    "cloud": "☁",   # ☁
    "rain": "☔",    # ☔
    "snow": "❄",    # ❄
    "storm": "⛈",   # ⛈
}


def weather_category(code: int) -> str:
    if code == 0:
        return "sun"
    if code in (1, 2):
        return "partly"
    if code in (3, 45, 48):
        return "cloud"
    if code in (95, 96, 99):
        return "storm"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    return "cloud"


def weather_glyph(code: int) -> str:
    return WEATHER_GLYPHS[weather_category(code)]


# ---------- Gustaf's voice ----------

QUOTE_POOLS: dict[str, object] = {
    "time": {
        "morning": [
            "Good morning, sir.", "A fine morning, sir.", "Rise and shine, sir.",
            "Tea is ready, sir.", "Up and at 'em, sir.", "The day awaits, sir.",
            "Mornings are for tea, sir.", "A bright start, sir.",
            "The kettle's on, sir.", "Stretch and yawn, sir.",
        ],
        "afternoon": [
            "Good afternoon, sir.", "A pleasant afternoon, sir.", "Carry on, sir.",
            "More tea, sir?", "The day persists, sir.", "A spot of lunch, sir?",
            "Steady as she goes, sir.", "Mid-day already, sir.",
            "A second wind awaits, sir.", "Pace yourself, sir.",
        ],
        "evening": [
            "Good evening, sir.", "A gentle evening, sir.", "Wind down, sir.",
            "Pipe and slippers, sir?", "The lamps are lit, sir.", "A nightcap, sir?",
            "Twilight, sir.", "The hearth is warm, sir.",
            "Dinner approaches, sir.", "Rest is earned, sir.",
        ],
    },
    "weekday": {
        0: ["Onward, sir.", "A new week, sir.", "Mind the Mondays, sir.",
            "Begin again, sir.", "A fresh slate, sir.", "Briskly now, sir.",
            "Mondays demand tea, sir.", "The week unfolds, sir."],
        1: ["A steady Tuesday, sir.", "Tuesdays are underrated, sir.",
            "Tea is poured, sir.", "Press on, sir.", "A modest Tuesday, sir.",
            "The week finds its rhythm, sir.", "A workmanlike day, sir.",
            "Quietly does it, sir."],
        2: ["The week's middle, sir.", "Halfway, sir.", "Hump day, sir.",
            "The crest is in sight, sir.", "A balanced Wednesday, sir.",
            "Tea, then onwards, sir.", "Wednesday holds the line, sir.",
            "Mid-week musings, sir."],
        3: ["Almost there, sir.", "A handsome Thursday, sir.",
            "The end approaches, sir.", "One more push, sir.",
            "Thursday holds promise, sir.", "The weekend stirs, sir.",
            "Steady, the finish line, sir.", "A penultimate effort, sir."],
        4: ["Happy Friday, sir.", "The weekend approaches, sir.",
            "Almost free, sir.", "A celebratory Friday, sir.",
            "The end is nigh, sir.", "Cheers to Friday, sir.",
            "The week concludes, sir.", "TGIF, sir."],
        5: ["A leisurely Saturday, sir.", "Rest well, sir.", "No alarms, sir.",
            "A gentle Saturday, sir.", "Take your time, sir.",
            "Saturdays are a gift, sir.", "A slow day, sir.", "Linger, sir."],
        6: ["A peaceful Sunday, sir.", "Pace yourself, sir.",
            "Sundays are sacred, sir.", "A gentle Sunday, sir.",
            "Roast and rest, sir.", "Sunday comforts, sir.",
            "A quiet day, sir.", "Sunday holds steady, sir."],
    },
    "wish": [
        "May your day be splendid, sir.", "Best wishes, sir.",
        "A fine day to you, sir.", "May fortune smile, sir.",
        "Have a pleasant one, sir.", "Wishing you well, sir.",
        "May the day treat you kindly, sir.", "A handsome day ahead, sir.",
        "Chin up, sir.", "May things go your way, sir.",
    ],
    "encourage": [
        "Almost there, sir.", "The finish is in sight, sir.", "Persevere, sir.",
        "Mustn't dawdle, sir.", "Nearly through, sir.", "Keep at it, sir.",
        "Bear up, sir.", "Steady on, sir.",
        "One foot, then the other, sir.", "You've earned a rest, sir.",
    ],
    "generic": [
        "At your service, sir.", "As you wish, sir.", "Indeed, sir.",
        "Quite right, sir.", "Tea, sir?", "Most agreeable, sir.",
        "Splendid, sir.", "By all means, sir.", "Naturally, sir.",
        "Of course, sir.",
    ],
    "weather": {
        "rain": [
            "Mind the rain, sir.", "Brolly weather, sir.", "A damp day, sir.",
            "Pitter-patter, sir.", "The skies weep, sir.", "Mac and wellies, sir.",
            "An indoor day, sir.", "The garden drinks, sir.",
        ],
        "storm": [
            "Stay indoors, sir.", "A wild one, sir.", "Best stay in, sir.",
            "The heavens roar, sir.", "Hold fast, sir.", "Tempestuous, sir.",
            "The wind howls, sir.", "An apocalyptic mood, sir.",
        ],
        "snow": [
            "A frosty day, sir.", "The flakes fall, sir.",
            "Snow boots required, sir.", "A wintry scene, sir.",
            "Cocoa weather, sir.", "The world is white, sir.",
            "Mind the ice, sir.", "A snow globe day, sir.",
        ],
        "sun": [
            "A glorious sun, sir.", "Splendid weather, sir.", "Sunglasses, sir?",
            "A radiant day, sir.", "The sun is generous, sir.", "Step lively, sir.",
            "A picnic mood, sir.", "The light is fine, sir.",
        ],
    },
    "temp": {
        "hot": [
            "Mind the heat, sir.", "Hydrate, sir.", "A scorcher, sir.",
            "Lemonade, sir?", "Find the shade, sir.", "Beastly hot, sir.",
            "Linen weather, sir.", "Mercury rising, sir.",
        ],
        "cold": [
            "A nip in the air, sir.", "Wear a scarf, sir.", "Mittens, sir.",
            "Brrr, sir.", "The fire's lit, sir.", "Wool weather, sir.",
            "Fingers and toes, sir.", "A frigid one, sir.",
        ],
    },
}

# Per-time-of-day weights for each pool category. Higher = more likely.
# A weight of 0 disables that category at that time.
QUOTE_WEIGHTS = {
    "morning":   {"time": 2, "weekday": 1, "wish": 3, "weather": 3, "temp": 2, "encourage": 0, "generic": 1},
    "afternoon": {"time": 2, "weekday": 1, "wish": 1, "weather": 1, "temp": 1, "encourage": 2, "generic": 1},
    "evening":   {"time": 2, "weekday": 1, "wish": 0, "weather": 0, "temp": 0, "encourage": 3, "generic": 1},
}


def pick_quote(now: datetime, today_weather: DailyWeather | None) -> str:
    rng = random.Random(now.toordinal())
    if now.hour < 12:
        tod = "morning"
    elif now.hour < 17:
        tod = "afternoon"
    else:
        tod = "evening"
    weights = QUOTE_WEIGHTS[tod]

    candidates: list[tuple[int, list[str]]] = [
        (weights["time"],     QUOTE_POOLS["time"][tod]),
        (weights["weekday"],  QUOTE_POOLS["weekday"][now.weekday()]),
        (weights["wish"],     QUOTE_POOLS["wish"]),
        (weights["encourage"], QUOTE_POOLS["encourage"]),
        (weights["generic"],  QUOTE_POOLS["generic"]),
    ]
    if today_weather:
        cat = weather_category(today_weather.code)
        if cat in QUOTE_POOLS["weather"]:
            candidates.append((weights["weather"], QUOTE_POOLS["weather"][cat]))
        if today_weather.tmax >= 30:
            candidates.append((weights["temp"], QUOTE_POOLS["temp"]["hot"]))
        elif today_weather.tmax <= 8:
            candidates.append((weights["temp"], QUOTE_POOLS["temp"]["cold"]))

    candidates = [(w, p) for w, p in candidates if w > 0 and p]
    pool = rng.choices([p for _, p in candidates],
                       weights=[w for w, _ in candidates], k=1)[0]
    return rng.choice(pool)


def pick_bear(now: datetime, today_weather: DailyWeather | None) -> tuple[str, str]:
    """Return (face, weather_suffix). Face is rendered in sans, suffix in symbols font."""
    rng = random.Random(now.toordinal() ^ 0x5EAB)
    h = now.hour
    if h < 7:
        face = "ʕ-ᴥ-ʔ"
    elif h >= 21:
        face = "ʕ°ᴥ°ʔ"
    else:
        face = rng.choice(["ʕ•ᴥ•ʔ", "ʕ◕ᴥ◕ʔ", "ʕ◔ᴥ◔ʔ", "ʕ•ᴥ•ʔ"])
    suffix = ""
    if today_weather:
        cat = weather_category(today_weather.code)
        if cat in ("rain", "storm"):
            suffix = "☂"
        elif cat == "snow":
            suffix = "❄"
        elif cat == "sun":
            suffix = "☀"
    return face, suffix


# ---------- decorative drawing helpers ----------

def dashed_line(d: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int,
                fill: int = 0, width: int = 1,
                dash: int = 4, gap: int = 3) -> None:
    """Draw a horizontal or vertical dashed line (stitched-seam look)."""
    if x0 == x1:
        y = y0
        while y < y1:
            d.line((x0, y, x0, min(y + dash, y1)), fill=fill, width=width)
            y += dash + gap
    else:
        x = x0
        while x < x1:
            d.line((x, y0, min(x + dash, x1), y0), fill=fill, width=width)
            x += dash + gap


def paw_prints_rect(img: Image.Image, x: int, y: int, w: int, h: int,
                    spacing: int = 24, seed: int = 0) -> None:
    """Tile small white paw prints (one pad + three toes) over a black rect."""
    d = ImageDraw.Draw(img)
    rng = random.Random(seed)
    for yy in range(y + 10, y + h - 8, spacing):
        offset = (yy // spacing) % 2 * (spacing // 2)
        for xx in range(x + 10 + offset, x + w - 8, spacing):
            jx = xx + rng.randint(-1, 1)
            jy = yy + rng.randint(-1, 1)
            d.ellipse((jx - 2, jy, jx + 2, jy + 4), fill=255)            # main pad
            d.ellipse((jx - 4, jy - 4, jx - 2, jy - 2), fill=255)        # left toe
            d.ellipse((jx - 1, jy - 5, jx + 1, jy - 3), fill=255)        # center toe
            d.ellipse((jx + 2, jy - 4, jx + 4, jy - 2), fill=255)        # right toe


def text_frame(d: ImageDraw.ImageDraw, bbox: tuple[int, int, int, int],
               pad: int = 5) -> None:
    """Clear (re-fill black) the bbox+pad region, then draw a white frame around it.
    Use to make text legible over a patterned black background."""
    x0, y0, x1, y1 = bbox
    fx0, fy0, fx1, fy1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    d.rectangle((fx0, fy0, fx1, fy1), fill=0)
    d.rectangle((fx0, fy0, fx1, fy1), outline=255, width=1)


def draw_layered_text(d: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
                      fonts: list[ImageFont.FreeTypeFont], fill: int = 0,
                      anchor_baseline: int | None = None) -> float:
    """Draw text character-by-character, picking the first font in `fonts` that has
    a glyph for each character. Returns the total advance width.
    anchor_baseline aligns characters to a common baseline (y in image coords)."""
    x, y = xy
    total = 0.0
    for ch in text:
        chosen = fonts[-1]
        for f in fonts:
            if f.getmask(ch).getbbox() is not None:
                chosen = f
                break
        if anchor_baseline is not None:
            ascent, _ = chosen.getmetrics()
            cy = anchor_baseline - ascent
        else:
            cy = y
        d.text((x + total, cy), ch, font=chosen, fill=fill)
        total += chosen.getlength(ch)
    return total


def fetch(src: Source) -> Calendar:
    r = requests.get(src.url, auth=src.auth, timeout=30)
    r.raise_for_status()
    return Calendar.from_ical(r.text)


def to_local(dt) -> datetime:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    return datetime.combine(dt, time(0, 0), tzinfo=TZ)


def collect(window_start: datetime, window_end: datetime) -> list[Event]:
    events: list[Event] = []
    failed = 0
    for src in SOURCES:
        try:
            cal = fetch(src)
        except Exception as e:
            print(f"warn: {src.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        for ev in recurring_ical_events.of(cal).between(window_start, window_end):
            raw_start = ev["DTSTART"].dt
            raw_end = ev.get("DTEND").dt if ev.get("DTEND") else raw_start
            all_day = not isinstance(raw_start, datetime)
            events.append(Event(
                start=to_local(raw_start),
                end=to_local(raw_end),
                summary=str(ev.get("SUMMARY", "(no title)")),
                source=src.name,
                all_day=all_day,
            ))
    if SOURCES and failed == len(SOURCES):
        # Nothing to show: exit non-zero before rendering so the display keeps
        # the last good image and the failure is visible (e.g. a failed Job).
        sys.exit(f"error: all {failed} calendar sources failed; not uploading")
    events.sort(key=lambda e: (e.start, e.all_day is False))
    return events


def columns_for(days: int) -> int:
    if days <= 2:
        return 1
    if days <= 6:
        return 2
    return 7


def draw_header(d: ImageDraw.ImageDraw, start_day: date, header_h: int,
                today_weather: DailyWeather | None, now: datetime) -> None:
    hero_day = ImageFont.truetype(FONT_GENTIUM_BOLD, 46)
    hero_num = ImageFont.truetype(FONT_GENTIUM_BOLD, 46)
    sub_font = ImageFont.truetype(FONT_GENTIUM_ITALIC, 18)
    bear_face_font = ImageFont.truetype(FONT_MONO, 20)
    bear_suffix_font = ImageFont.truetype(FONT_SYMBOLS, 18)
    bubble_font = ImageFont.truetype(FONT_GENTIUM_ITALIC, 16)
    temp_font = ImageFont.truetype(FONT_BOLD, 13)
    weather_font = ImageFont.truetype(FONT_SYMBOLS, 22)

    weekday = f"{start_day:%A}"  # full name reads better in serif
    daynum = f"{start_day:%-d}"
    weekday_w = d.textlength(weekday, font=hero_day)
    d.text((14, 0), weekday, font=hero_day, fill=0)
    d.text((14 + weekday_w + 12, 0), daynum, font=hero_num, fill=0)

    d.text((16, 56), "Gustaf's agenda", font=sub_font, fill=0)

    face, suffix = pick_bear(now, today_weather)
    face_w = d.textlength(face, font=bear_face_font)
    suffix_w = d.textlength(suffix, font=bear_suffix_font) if suffix else 0
    bear_x = WIDTH - int(face_w + suffix_w) - 14
    bear_y = header_h // 2 - 14
    d.text((bear_x, bear_y), face, font=bear_face_font, fill=0)
    if suffix:
        d.text((bear_x + face_w, bear_y), suffix, font=bear_suffix_font, fill=0)

    msg = pick_quote(now, today_weather)
    msg_w = d.textlength(msg, font=bubble_font)

    glyph = weather_glyph(today_weather.code) if today_weather else ""
    glyph_w = d.textlength(glyph, font=weather_font) if glyph else 0
    temp_str = (f"{round(today_weather.tmin)}° / {round(today_weather.tmax)}°"
                if today_weather else "")
    temp_w = d.textlength(temp_str, font=temp_font) if temp_str else 0
    weather_w = (glyph_w + 6 + temp_w) if today_weather else 0

    pad_x, pad_y = 12, 6
    inner_w = max(msg_w, weather_w)
    bubble_h = 54 if today_weather else 30
    b_right = bear_x - 12
    b_left = int(b_right - inner_w - pad_x * 2)
    b_top = header_h // 2 - bubble_h // 2
    b_bottom = b_top + bubble_h
    d.rounded_rectangle((b_left, b_top, b_right, b_bottom),
                        radius=10, outline=0, fill=255, width=1)
    d.text((b_left + pad_x, b_top + pad_y - 2), msg, font=bubble_font, fill=0)
    if today_weather:
        gx = b_left + pad_x
        gy = b_top + pad_y + 18
        d.text((gx, gy), glyph, font=weather_font, fill=0)
        d.text((gx + glyph_w + 6, gy + 6), temp_str, font=temp_font, fill=0)

    tail_apex_y = b_top + (b_bottom - b_top) // 2
    tail = [(b_right - 1, tail_apex_y - 4),
            (b_right + 7, tail_apex_y),
            (b_right - 1, tail_apex_y + 4)]
    d.polygon(tail, fill=255, outline=0)
    d.line((b_right, tail_apex_y - 4, b_right, tail_apex_y + 4), fill=255, width=1)

    dashed_line(d, 0, header_h, WIDTH, header_h)


def draw_cell(img: Image.Image, d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
              day: date, day_events: list[Event],
              day_weather: DailyWeather | None, top_header: bool) -> None:
    day_font = ImageFont.truetype(FONT_BOLD, 14)
    day_num = ImageFont.truetype(FONT_GENTIUM_BOLD, 26)
    weather_small = ImageFont.truetype(FONT_SYMBOLS, 18)
    body = ImageFont.truetype(FONT_REG, 13)
    body_bold = ImageFont.truetype(FONT_BOLD, 13)

    if top_header:
        stripe_h = 38
        d.rectangle((x, y, x + w, y + stripe_h), fill=0)
        paw_prints_rect(img, x, y, w, stripe_h, spacing=20, seed=day.toordinal())
        text_frame(d, (x + 6, y + 3, x + w - 6, y + stripe_h - 3), pad=2)
        d.text((x + 8, y + 4), f"{day:%a}", font=day_font, fill=255)
        num_w = d.textlength(f"{day:%-d}", font=day_num)
        d.text((x + w - num_w - 8, y + 4), f"{day:%-d}", font=day_num, fill=255)
        ex, ey = x + 6, y + stripe_h + 6
        max_chars = 22
    else:
        stripe_w = 90
        d.rectangle((x, y, x + stripe_w, y + h), fill=0)
        paw_prints_rect(img, x, y, stripe_w, h, spacing=22, seed=day.toordinal())
        text_x = x + 10
        day_str = f"{day:%a}"
        num_str = f"{day:%-d}"
        lines: list[tuple[int, ImageFont.FreeTypeFont, str]] = [
            (y + 8, day_font, day_str),
            (y + 24, day_num, num_str),
        ]
        if day_weather:
            lines.append((y + 56, weather_small, weather_glyph(day_weather.code)))
            lines.append((y + 80, body,
                          f"{round(day_weather.tmin)}/{round(day_weather.tmax)}°"))
        # tight bbox around the actual rendered glyph extents
        line_bboxes = [d.textbbox((text_x, ly), ls, font=lf)
                       for ly, lf, ls in lines]
        bbox_top = min(b[1] for b in line_bboxes)
        bbox_bottom = max(b[3] for b in line_bboxes)
        bbox_right = max(b[2] for b in line_bboxes)
        text_frame(d, (text_x - 1, bbox_top - 2,
                       bbox_right + 1, bbox_bottom + 2), pad=3)
        for ly, lf, ls in lines:
            d.text((text_x, ly), ls, font=lf, fill=255)
        ex, ey = x + stripe_w + 10, y + 8
        max_chars = 60 if w > 500 else 32

    content_bottom = y + h - 4
    for ev in day_events:
        if ey > content_bottom - 16:
            d.text((ex, ey), "…", font=body, fill=0)
            break
        if ev.all_day:
            d.text((ex, ey), ev.summary[:max_chars], font=body_bold, fill=0)
        else:
            timestr = f"{ev.start:%H:%M} "
            d.text((ex, ey), timestr, font=body, fill=0)
            tw = d.textlength(timestr, font=body)
            d.text((ex + tw, ey), ev.summary[:max_chars], font=body, fill=0)
        ey += 18


def render(events: list[Event], start_day: date, now: datetime) -> Image.Image:
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    d = ImageDraw.Draw(img)

    weather = fetch_weather(start_day, DAYS)

    header_h = 90
    draw_header(d, start_day, header_h, weather.get(start_day), now)

    cols = columns_for(DAYS)
    rows = math.ceil(DAYS / cols)
    cell_w = WIDTH // cols
    cell_h = (HEIGHT - header_h) // rows
    top_header = cols >= 5

    for i in range(DAYS):
        # Column-major: fill the left column top-to-bottom, then the next.
        col = i // rows
        row = i % rows
        x = col * cell_w
        y = header_h + row * cell_h
        if col:
            dashed_line(d, x, y, x, y + cell_h)
        if row:
            dashed_line(d, x, y, x + cell_w, y)
        day = start_day + timedelta(days=i)
        day_events = [e for e in events if e.start.date() == day]
        draw_cell(img, d, x, y, cell_w, cell_h, day, day_events,
                  weather.get(day), top_header)

    return img


def upload(img: Image.Image) -> None:
    img.save(OUTPUT, "PNG", optimize=True)
    if not R2_BUCKET:
        return
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=R2_KEY,
        Body=buf.getvalue(),
        ContentType="image/png",
        CacheControl="no-cache",
    )
    print(f"uploaded to r2://{R2_BUCKET}/{R2_KEY}")


def main() -> None:
    now = datetime.now(TZ)
    today = now.date()
    start = datetime.combine(today, time(0, 0), tzinfo=TZ)
    end = start + timedelta(days=DAYS)
    events = collect(start, end)
    img = render(events, today, now)
    upload(img)
    print(f"wrote {OUTPUT} with {len(events)} events")


if __name__ == "__main__":
    main()
