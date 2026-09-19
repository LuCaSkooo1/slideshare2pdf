#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests", "pillow", "textual>=1.0", "img2pdf"]
# ///
"""
slideshare2pdf.py - download a SlideShare presentation as one PDF (terminal UI)

Paste a presentation link, for example
    https://www.slideshare.net/slideshow/netflix-casestudyfina-lv2/24397003
The script reads the page, finds the slide images the viewer shows
    https://image.slidesharecdn.com/<deck>/75/<Title>-<n>-2048.jpg
downloads them in parallel and merges them into a single PDF.

SlideShare sometimes answers a script with a bot check. The script then tries
the embed player and the oEmbed API, and finally opens the page in a hidden
Chrome/Chromium window (--dump-dom), which runs the check's JavaScript.
If curl_cffi is installed it is used for the page, which often passes too.

Instead of a link you can also give:
    - the address of any single slide image (image.slidesharecdn.com/...)
    - the embed code copied from the Share/Embed dialog
    - a page saved from the browser, or any pasted HTML holding a slide image
The number of slides is then found by probing the CDN.

Run with uv (installs the dependencies by itself):
    uv run slideshare2pdf.py
or in a virtual environment (Python 3.9+):
    python3 -m venv ~/.venvs/slideshare
    ~/.venvs/slideshare/bin/pip install requests pillow textual img2pdf
    ~/.venvs/slideshare/bin/python slideshare2pdf.py
(img2pdf is optional: it puts the JPGs into the PDF losslessly, without it
 Pillow re-encodes them)

Usage:
    slideshare2pdf.py                    # TUI
    slideshare2pdf.py LINK               # TUI, starts right away
    slideshare2pdf.py LINK --no-tui -o deck.pdf --size 2048 --slides 1-10
"""

from __future__ import annotations

import argparse
import html
import json
import re
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, unquote, urlparse

try:
    import requests
    from PIL import Image
    from rich.markup import escape
    from rich.text import Text
    from textual import events, on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.theme import Theme
    from textual.widgets import (Button, Checkbox, Footer, Header, Input, Label,
                                 ProgressBar, RichLog, Select, Static)
except ImportError as exc:
    sys.exit(f"Missing or outdated Python package: {exc.name or exc}\n\n"
             "Easiest:  uv run slideshare2pdf.py      (uv installs everything by itself)\n"
             "or:       python3 -m venv ~/.venvs/slideshare\n"
             "          ~/.venvs/slideshare/bin/pip install requests pillow textual img2pdf\n"
             "          ~/.venvs/slideshare/bin/python slideshare2pdf.py")

try:
    import img2pdf  # optional: lossless JPG -> PDF
except ImportError:
    img2pdf = None

try:
    # optional: sends the TLS fingerprint of a real browser, which is often
    # enough to get past SlideShare's bot check without starting a browser
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


# --------------------------------------------------------------------------
# SlideShare specifics
# --------------------------------------------------------------------------
CDN = "https://image.slidesharecdn.com"
SIZES = (2048, 1024, 638, 320)                        # slide widths the CDN serves
DEFAULT_Q = {2048: 75, 1024: 75, 638: 85, 320: 85}    # the /75/ or /85/ part of the path
PAGE_WIDTH_PT = 960                                   # PDF page width = 16:9 PowerPoint slide
MAX_SLIDES = 3000                                     # sanity limit when counting slides

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
PAGE_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
IMG_HEADERS = {"User-Agent": UA, "Referer": "https://www.slideshare.net/",
               "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}

_CDN_RX = r"(?:https?:)?//image\.slidesharecdn\.com/(?P<key>[^/\s\"'<>]+)/(?P<q>\d+)/"
# .../<key>/<q>/<Title-slug>-<n>-<width>.jpg   (img src / srcset)
FULL_RE = re.compile(_CDN_RX + r"(?P<slug>[^/\s\"'<>?#]*)-(?P<n>\d+)-(?P<w>\d+)"
                               r"\.(?P<ext>jpe?g|png|webp)", re.I)
# .../<key>/<q>/<Title-slug>-<n>                 (JSON base URL without the size)
BASE_RE = re.compile(_CDN_RX + r"(?P<slug>[^/\s\"'<>?#.]*)-(?P<n>\d+)(?=[\"'\s,?<>\\]|$)", re.I)
TOTAL_RE = re.compile(r'"(?:totalSlides|total_slides|slideCount|numberOfPages)"\s*:\s*"?(\d+)')
EMBED_RE = re.compile(r"(?:https?:)?//(?:www\.)?slideshare\.net/slideshow/embed_code/(?:key/)?[\w-]+")
THUMB_RE = re.compile(r"ss_thumbnails/([^/?#\"'\s]+?)-thumbnail")      # og:image names the deck
CHALLENGE_RE = re.compile(r"<title>\s*(?:Client Challenge|Just a moment|Attention Required)", re.I)
# a name never contains braces or quotes - that would be the page's own JSON data
AUTHOR_RES = (re.compile(r'"(?:author|user|uploader)"\s*:\s*\{[^{}]{0,300}?'
                         r'"(?:name|displayName|fullName)"\s*:\s*"([^"{}]{1,80})"'),
              re.compile(r"Uploaded by(?:\s|<!--.*?-->|<[^>]*>)*([^<>{}\"]{1,80}?)\s*<", re.S),
              re.compile(r"</strong>\s*from\s*<strong>\s*<a[^>]*>([^<{}\"]{1,80})</a>", re.I))
SHARE_URL_RE = re.compile(r"https?://[\w.-]*slideshare\.net/[^\s\"'<>\\)]+", re.I)

# headless Chrome/Chromium runs the page's JavaScript, so it passes the bot check
BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
            "brave-browser", "microsoft-edge", "microsoft-edge-stable", "vivaldi")
MAC_BROWSERS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")

PASTE_HINT = ("open the presentation in your browser, right-click a slide, choose "
              "'Copy image address' and paste that link here")
BROWSER_HINT = ("install Chromium or Chrome (then the page is opened in a hidden browser "
                "window, which gets past the check)")
BLOCKED_MSG = "SlideShare showed a bot check instead of the presentation."
NO_SLIDES_MSG = ("No slide images found on this page. If the presentation opens in your browser, "
                 + PASTE_HINT + ".")
NO_TEXT_MSG = ("No slide images and no SlideShare link in that text. Paste a presentation link, "
               "the embed code, or the address of one slide image.")

Log = Callable[[str, str], None]          # log(level, message), level: info | ok | warn | error


def _no_log(level: str, msg: str) -> None:
    pass


class ResolveError(RuntimeError):
    """The link could not be turned into a list of slide images."""


class Blocked(ResolveError):
    """SlideShare answered with a bot check (or HTTP 403) instead of the page."""


@dataclass
class Deck:
    """Everything needed to build the image URL of any slide."""
    source: str                  # the link the user gave
    key: str                     # e.g. netflix-case-study-finalv2-130718141658-phpapp02
    slug: str                    # e.g. Netflix-case-study
    ext: str = "jpg"
    title: str = ""
    author: str = ""
    total: int = 0
    seen: int = 0                # highest slide number found on the page
    probe_width: int = 320       # size used to test whether a slide exists
    quality: dict[int, int] = field(default_factory=dict)             # width -> /q/ segment
    known: dict[tuple[int, int], str] = field(default_factory=dict)   # (slide, width) -> URL

    def url(self, n: int | str, width: int, q: Optional[int] = None) -> str:
        q = q or self.quality.get(width) or DEFAULT_Q.get(width, 75)
        return f"{CDN}/{self.key}/{q}/{self.slug}-{n}-{width}.{self.ext}"

    def candidates(self, n: int, width: int) -> list[str]:
        """URLs to try for slide n: the wanted size first, the other sizes as a fallback."""
        qs = [self.quality.get(width), DEFAULT_Q.get(width),
              *sorted(set(self.quality.values()) | {75, 85})]
        urls = [self.known.get((n, width))] + [self.url(n, width, q) for q in qs if q]
        for w in sorted(set(SIZES) | set(self.quality), reverse=True):
            if w != width:
                urls += [self.known.get((n, w)), self.url(n, w)]
        return [u for u in dict.fromkeys(urls) if u]


# --------------------------------------------------------------------------
# Reading the page
# --------------------------------------------------------------------------
_local = threading.local()


def http() -> requests.Session:
    """One requests.Session per thread (sessions are not thread-safe)."""
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
    return session


def normalize_link(link: str) -> str:
    url = link.strip().strip("<>\"' ")
    if url.startswith("//"):
        url = "https:" + url
    elif not re.match(r"[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    return url.split("#", 1)[0]


def _unescape(text: str) -> str:
    for escaped in ("\\u002F", "\\u002f", "\\/"):
        text = text.replace(escaped, "/")
    return html.unescape(text)


def deck_from_text(text: str, source: str, prefer: str = "") -> Optional[Deck]:
    """Find slide image URLs in HTML/JSON (or a single URL) and derive the URL pattern."""
    text = _unescape(text)
    hits = []  # (key, q, slug, slide, width or 0, ext, url)
    for m in FULL_RE.finditer(text):
        url = "https://" + m.group(0).split("//", 1)[1]
        hits.append((m["key"], int(m["q"]), m["slug"], int(m["n"]), int(m["w"]),
                     m["ext"].lower(), url))
    for m in BASE_RE.finditer(text):
        hits.append((m["key"], int(m["q"]), m["slug"], int(m["n"]), 0, "", ""))
    if not hits:
        return None

    # The page also shows other decks. Ours is the one named by the page's
    # preview image, otherwise the one with the most slides on the page.
    slides_of: dict[str, set[int]] = {}
    for h in hits:
        slides_of.setdefault(h[0], set()).add(h[3])
    key = prefer if prefer in slides_of else max(slides_of, key=lambda k: len(slides_of[k]))
    mine = [h for h in hits if h[0] == key]

    exts = Counter(h[5] for h in mine if h[5]).most_common(1)
    deck = Deck(source=source, key=key,
                slug=Counter(h[2] for h in mine).most_common(1)[0][0],
                ext=exts[0][0] if exts else "jpg",
                seen=max(h[3] for h in mine))
    q_seen: dict[int, Counter] = {}
    for _key, q, _slug, n, w, _ext, url in mine:
        if w:
            q_seen.setdefault(w, Counter())[q] += 1
            deck.known.setdefault((n, w), url)
    deck.quality = {w: c.most_common(1)[0][0] for w, c in q_seen.items()}
    if deck.quality:
        deck.probe_width = min(deck.quality)
    return deck


def _meta(page: str, name: str) -> str:
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        if re.search(r"""(?:property|name)\s*=\s*["']%s["']""" % re.escape(name), tag, re.I):
            m = re.search(r"""content\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag, re.I)
            if m:
                return html.unescape(m.group(1) if m.group(1) is not None else m.group(2)).strip()
    return ""


def _clean(text: str, limit: int) -> str:
    """One tidy line: pages sometimes hand out half of their JSON data."""
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def page_title(page: str) -> str:
    title = ""
    for name in ("og:title", "title", "twitter:title"):
        title = _meta(page, name)
        if title and title.lower() != "slideshare":
            break
    else:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = m.group(1) if m else ""
    title = re.sub(r"\s*\|\s*(?:PDF|PPTX?|SlideShare|Free Download)\b.*$", "",
                   _clean(title, 150), flags=re.I)
    return "" if title.lower() == "slideshare" else title


def page_author(page: str) -> str:
    """The name under the presentation - never a piece of the page's JSON."""
    for source in [_meta(page, "author")] + [m.group(1) for m in
                                             filter(None, (rx.search(page) for rx in AUTHOR_RES))]:
        name = _clean(source, 80)
        if name and not re.search(r"""[{}"<>\\]|\bhttps?:|\\u00""", name):
            return name
    return ""


def title_from_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", unquote(slug)).strip() or "slideshare"


def _preferred_key(page: str) -> str:
    """The deck key in the page's preview image (…/ss_thumbnails/<key>-thumbnail.jpg)."""
    m = THUMB_RE.search(_meta(page, "og:image") or _meta(page, "twitter:image"))
    return m.group(1) if m else ""


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _deck_from_oembed(data: dict, source: str) -> Optional[Deck]:
    """SlideShare's oEmbed answer: slide n = slide_image_baseurl + n + slide_image_baseurl_suffix."""
    base, suffix = data.get("slide_image_baseurl"), data.get("slide_image_baseurl_suffix")
    if isinstance(base, str) and isinstance(suffix, str):
        return deck_from_text(f"{base}1{suffix}", source)
    return None


def _net_error(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.ConnectionError):
        return "no connection"
    return type(exc).__name__


def _check(stop: threading.Event) -> None:
    if stop.is_set():
        raise ResolveError("Stopped.")


def _get_page(url: str):
    """One GET of a slideshare.net page, through curl_cffi if it is installed."""
    if curl_requests is not None:
        try:
            return curl_requests.get(url, headers=PAGE_HEADERS, timeout=25, impersonate="chrome")
        except Exception:                # any curl_cffi problem: fall back to requests
            pass
    return http().get(url, headers=PAGE_HEADERS, timeout=25)


def find_browser() -> str:
    """Path of an installed Chrome/Chromium, or "" if there is none."""
    for name in BROWSERS:
        exe = shutil.which(name)
        if exe:
            return exe
    return next((app for app in MAC_BROWSERS if Path(app).exists()), "")


def _kill(proc: "subprocess.Popen") -> None:
    """Kill the browser and everything it started."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def browser_page(url: str, log: Log, stop: Optional[threading.Event] = None,
                 seconds: int = 90) -> str:
    """Load the page in a hidden Chrome/Chromium window and return the finished HTML.

    The bot check is a piece of JavaScript, so a real browser engine simply passes it.
    """
    exe = find_browser()
    if not exe:
        return ""
    log("info", f"Opening the page in {Path(exe).name} (hidden window)…")
    with tempfile.TemporaryDirectory(prefix="slideshare2pdf-browser-") as profile:
        cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
               "--no-first-run", "--no-default-browser-check", "--disable-extensions",
               f"--user-data-dir={profile}", f"--user-agent={UA}", "--window-size=1280,900",
               "--lang=en-US", "--virtual-time-budget=25000", "--timeout=40000",
               "--dump-dom", url]
        if getattr(os, "geteuid", lambda: 1)() == 0:
            cmd.insert(1, "--no-sandbox")            # Chrome refuses to run as root otherwise
        try:                             # own process group, so children die with it
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    **({"start_new_session": True} if os.name == "posix" else {}))
        except OSError as exc:
            log("warn", f"The browser could not be started ({type(exc).__name__})")
            return ""
        deadline = time.monotonic() + seconds
        while True:                      # wait, but stay interruptible
            try:
                out = proc.communicate(timeout=0.5)[0]
                break
            except subprocess.TimeoutExpired:
                if (stop is not None and stop.is_set()) or time.monotonic() > deadline:
                    _kill(proc)
                    log("warn", "The browser was stopped" if stop and stop.is_set()
                        else "The browser took too long")
                    return ""
    page = (out or b"").decode("utf-8", "replace")
    if CHALLENGE_RE.search(page[:5000]):
        log("warn", "The browser got the bot check as well")
        return ""
    return page


def fetch_page(url: str) -> str:
    """GET a slideshare.net page. Raises Blocked for a bot check, ResolveError for other errors."""
    error = "no response"
    for attempt in range(3):
        try:
            r = _get_page(url)
        except requests.RequestException as exc:
            error = _net_error(exc)
        else:
            if r.status_code == 403 or CHALLENGE_RE.search(r.text[:5000]):
                raise Blocked(BLOCKED_MSG)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 410):
                raise ResolveError(f"SlideShare says this presentation does not exist "
                                   f"(HTTP {r.status_code}). Check the link.")
            if r.status_code < 500 and r.status_code != 429:
                raise ResolveError(f"SlideShare answered HTTP {r.status_code}.")
            error = f"HTTP {r.status_code}"
        time.sleep(1.5 * (attempt + 1))
    raise ResolveError(f"Could not load the page ({error}).")


def slide_exists(deck: Deck, n: int, stop: threading.Event) -> bool:
    """Is slide n on the CDN? Only a clear answer counts: an image = yes, HTTP 4xx = no."""
    url = deck.known.get((n, deck.probe_width)) or deck.url(n, deck.probe_width)
    problem = "no response"
    for attempt in range(4):
        _check(stop)
        try:
            with http().get(url, headers=IMG_HEADERS, timeout=15, stream=True) as r:
                if r.status_code == 200:
                    return not r.headers.get("Content-Type", "").startswith("text/")
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    return False
                problem = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            problem = _net_error(exc)
        time.sleep(1 + attempt)
    raise ResolveError(f"The CDN does not answer ({problem}), so the slides could not be "
                       "counted. Try again in a moment.")


def probe_total(deck: Deck, lo: int, hi: Optional[int], stop: threading.Event) -> int:
    """Number of the last slide on the CDN.

    Slide `lo` is checked first; `hi`, if given, is a slide number known to be missing.
    """
    if not slide_exists(deck, lo, stop):
        if lo == 1:
            raise ResolveError("The CDN does not have the first slide. Is the link complete?")
        return probe_total(deck, 1, lo, stop)
    if hi is None:                       # gallop: lo+1, lo+2, lo+4, … until one is missing
        step = 1
        while lo + step <= MAX_SLIDES and slide_exists(deck, lo + step, stop):
            lo += step
            step *= 2
        hi = lo + step
    while hi - lo > 1:                   # then binary search
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if slide_exists(deck, mid, stop) else (lo, mid)
    return lo


def find_total(deck: Deck, totals: list[int], log: Log, stop: threading.Event) -> int:
    """Number of slides: the page's own number if the images agree with it, else ask the CDN."""
    if deck.seen and deck.seen in totals:            # page data and images agree
        return deck.seen
    lo, hi = max(deck.seen, 1), None                 # slide lo exists, slide hi does not
    for guess in list(dict.fromkeys(totals))[:5]:    # the page shows fewer slides than it has
        if guess <= lo or (hi is not None and guess >= hi):
            continue
        if not slide_exists(deck, guess, stop):
            hi = guess
        elif not slide_exists(deck, guess + 1, stop):
            return guess
        else:
            lo = guess + 1
    log("info", "Counting the slides on the CDN…")
    return probe_total(deck, lo, hi, stop)


def _slideshow_id(url: str) -> str:
    m = re.search(r"/(\d{4,})(?:/|$)", urlparse(url).path)
    return m.group(1) if m else ""


def _alternatives(url: str, page: str) -> list[tuple[str, str]]:
    """Other addresses that show the same slides, for when the page itself does not."""
    if "/embed_code/" in url:
        return []
    alts = []
    embed = EMBED_RE.search(_unescape(page))
    if embed:
        alts.append(("embed player", normalize_link(embed.group(0))))
    if _slideshow_id(url):
        alts.append(("embed player",
                     f"https://www.slideshare.net/slideshow/embed_code/{_slideshow_id(url)}"))
    alts.append(("oEmbed API", "https://www.slideshare.net/api/oembed/2?format=json&url="
                 + quote(url, safe="")))
    return alts


def _read_slideshare(url: str, log: Log, stop: threading.Event,
                     browser: bool = True) -> tuple[Deck, list[int]]:
    """Read a slideshare.net page (or its embed player, or a hidden browser window)."""
    log("info", f"Reading {url}")
    page, blocked = "", False
    try:
        page = fetch_page(url)
    except Blocked:
        blocked = True
        log("warn", "SlideShare answered with a bot check")
    docs = [page] if page else []
    deck = deck_from_text(page, url, _preferred_key(page)) if page else None

    queue, tried = _alternatives(url, page), {url}
    while deck is None and queue:
        label, alt = queue.pop(0)
        if alt in tried:
            continue
        tried.add(alt)
        _check(stop)
        log("info", f"Trying the {label}…")
        try:
            text = fetch_page(alt)
        except Blocked:
            blocked = True
            continue
        except ResolveError:
            continue
        docs.append(text)
        deck = deck_from_text(text, url, _preferred_key(text)) or _deck_from_oembed(_json(text), url)
        queue += [("embed player", normalize_link(m.group(0)))
                  for m in EMBED_RE.finditer(_unescape(text))]

    if deck is None and browser:                     # let a real browser do the work
        _check(stop)
        rendered = browser_page(url, log, stop)
        if rendered:
            page, docs = rendered, docs + [rendered]
            deck = deck_from_text(rendered, url, _preferred_key(rendered))
            if deck is not None:
                log("ok", "The browser got the page")

    if deck is None:
        if blocked:
            extra = "" if find_browser() or not browser else f" {BROWSER_HINT},"
            raise Blocked(f"{BLOCKED_MSG} Try again in a minute,{extra} or {PASTE_HINT}.")
        raise ResolveError(NO_SLIDES_MSG)

    oembed = next((d for d in map(_json, docs) if d), {})
    deck.title = (page_title(page) if page else "") or str(oembed.get("title") or "").strip() \
        or title_from_slug(deck.slug)
    deck.author = (page_author(page) if page else "") or str(oembed.get("author_name") or "").strip()
    return deck, [int(t) for doc in docs for t in TOTAL_RE.findall(doc)]


def _pasted_text(link: str, log: Log) -> tuple[str, str]:
    """(text, where it came from) if the input is a saved page or pasted HTML, else ("", "")."""
    raw = link.strip()
    if not raw.lower().startswith(("http://", "https://")) and len(raw) < 4096:
        try:
            path = Path(raw.strip("'\"")).expanduser()
            if path.is_file():
                log("info", f"Reading {path.name}")
                return path.read_text("utf-8", errors="replace"), path.name
        except OSError:
            pass
    if re.search(r"[\s<>]", raw):                      # pasted embed code or page source
        return raw, "the pasted text"
    return "", ""


def resolve(link: str, log: Log = _no_log, stop: Optional[threading.Event] = None,
            browser: bool = True) -> Deck:
    """Turn a SlideShare link, a slide image link, a saved page or pasted HTML into a Deck."""
    stop = stop or threading.Event()
    totals: list[int] = []
    deck = None

    text, where = _pasted_text(link, log)
    if text:
        deck = deck_from_text(text, where, _preferred_key(text))
        if deck is not None:
            deck.title = page_title(text) or title_from_slug(deck.slug)
            deck.author = page_author(text)
            totals = [int(t) for t in TOTAL_RE.findall(text)]
            log("ok", f"Found slide images in {where}")
        else:
            found = SHARE_URL_RE.search(_unescape(text))
            if found is None:
                raise ResolveError(NO_TEXT_MSG)
            link = found.group(0)
            log("info", f"Using the link from {where}: {link}")

    if deck is None:
        url = normalize_link(link)
        host = (urlparse(url).hostname or "").lower()
        if host.endswith("slidesharecdn.com"):
            deck = deck_from_text(url, url)
            if deck is None:
                raise ResolveError("This image link does not look like a slide "
                                   "(…/<Title>-<number>-<width>.jpg).")
            deck.title = title_from_slug(deck.slug)
            log("info", "Link to one slide image: counting the slides on the CDN…")
        elif host == "slideshare.net" or host.endswith(".slideshare.net"):
            deck, totals = _read_slideshare(url, log, stop, browser)
        else:
            raise ResolveError("That is not a slideshare.net link. Paste a presentation address, "
                               "e.g. https://www.slideshare.net/slideshow/<name>/<id>")

    deck.total = find_total(deck, totals, log, stop)
    return deck


# --------------------------------------------------------------------------
# Downloading and building the PDF
# --------------------------------------------------------------------------
@dataclass
class JobResult:
    pdf: Optional[Path]
    pages: int
    failed: list[int]
    method: str = ""
    cancelled: bool = False


def _is_image(data: bytes) -> bool:
    return (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:4] == b"GIF8" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _width_of(url: str) -> int:
    m = re.search(r"-(\d+)\.\w+(?:\?.*)?$", url)
    return int(m.group(1)) if m else 0


def download_slides(deck: Deck, slides: list[int], width: int, folder: Path, *, jobs: int = 6,
                    progress: Optional[Callable[[int, int], None]] = None, log: Log = _no_log,
                    stop: Optional[threading.Event] = None) -> tuple[list[Path], list[int]]:
    """Download the slides in parallel. Returns (image paths in slide order, failed slide numbers)."""
    folder.mkdir(parents=True, exist_ok=True)
    stop = stop or threading.Event()

    def fetch(n: int) -> tuple[int, Optional[Path], str]:
        for url in deck.candidates(n, width):
            for attempt in range(3):
                if stop.is_set():
                    return n, None, ""
                try:
                    r = http().get(url, headers=IMG_HEADERS, timeout=30)
                except requests.RequestException:
                    time.sleep(1 + attempt)
                    continue
                if r.status_code == 200 and _is_image(r.content):
                    path = folder / f"{n:04d}{Path(urlparse(url).path).suffix or '.jpg'}"
                    path.write_bytes(r.content)
                    return n, path, url
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                break                    # 403/404: this variant does not exist, try the next one
        return n, None, ""

    got: dict[int, Path] = {}
    failed: list[int] = []
    pool = ThreadPoolExecutor(max_workers=max(1, jobs))
    futures = [pool.submit(fetch, n) for n in slides]
    try:
        for done, future in enumerate(as_completed(futures), 1):
            n, path, url = future.result()
            if path:
                got[n] = path
                used = _width_of(url)
                if used and used != width:
                    log("warn", f"Slide {n}: {width} px is not available, used {used} px")
                else:
                    log("ok", f"Slide {n}  ({path.stat().st_size // 1024} KB)")
            elif not stop.is_set():
                failed.append(n)
                log("error", f"Slide {n} could not be downloaded")
            if progress:
                progress(done, len(slides))
            if stop.is_set():
                break
    except BaseException:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return [got[n] for n in slides if n in got], sorted(failed)


def _image_width(path: Path) -> int:
    with Image.open(path) as im:
        return im.width


def _page_layout(width_px: int, height_px: int, _ndpi) -> tuple[float, float, float, float]:
    """img2pdf layout: every page is PAGE_WIDTH_PT wide, the height follows the image."""
    height = PAGE_WIDTH_PT * height_px / width_px
    return PAGE_WIDTH_PT, height, PAGE_WIDTH_PT, height


def build_pdf(images: list[Path], out: Path, title: str = "", author: str = "",
              subject: str = "") -> str:
    """Merge the images into one PDF. Returns a short description of the method used."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    try:
        if img2pdf is not None:
            try:
                with open(part, "wb") as fh:
                    img2pdf.convert([str(p) for p in images], layout_fun=_page_layout,
                                    outputstream=fh, title=title or None,
                                    author=author or None, subject=subject or None)
                part.replace(out)
                return "img2pdf, lossless"
            except Exception:
                pass                     # e.g. a WebP image: let Pillow do it
        # Pillow uses one resolution for all pages, so a slide that only came in
        # a smaller size is scaled up to keep every page equally wide.
        width = Counter(_image_width(p) for p in images).most_common(1)[0][0]
        lanczos = getattr(Image, "Resampling", Image).LANCZOS
        pages = []
        for p in images:
            page = Image.open(p).convert("RGB")
            if page.width != width:
                page = page.resize((width, round(page.height * width / page.width)), lanczos)
            pages.append(page)
        pages[0].save(part, "PDF", save_all=True, append_images=pages[1:],
                      resolution=width * 72 / PAGE_WIDTH_PT,
                      title=title, author=author, subject=subject)
        part.replace(out)
        return "Pillow, re-encoded"
    finally:
        part.unlink(missing_ok=True)


def run_job(deck: Deck, slides: list[int], width: int, out: Path, keep_images: bool = False, *,
            jobs: int = 6, progress: Optional[Callable[[int, int], None]] = None,
            log: Log = _no_log, stop: Optional[threading.Event] = None) -> JobResult:
    """Download the slides and write the PDF. Images go to a temp folder unless kept."""
    stop = stop or threading.Event()
    folder = (out.with_name(out.stem + "_slides") if keep_images
              else Path(tempfile.mkdtemp(prefix="slideshare2pdf-")))
    try:
        images, failed = download_slides(deck, slides, width, folder, jobs=jobs,
                                         progress=progress, log=log, stop=stop)
        if stop.is_set():
            return JobResult(None, 0, failed, cancelled=True)
        if not images:
            return JobResult(None, 0, failed)
        log("info", f"Writing the PDF ({len(images)} pages)…")
        method = build_pdf(images, out, title=deck.title, author=deck.author, subject=deck.source)
        if keep_images:
            log("info", f"Images kept in {folder}")
        return JobResult(out, len(images), failed, method)
    finally:
        if not keep_images:
            shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def parse_slides(spec: str, total: int) -> list[int]:
    """'' or 'all' -> every slide; '1-10, 15, 30-' -> 1..10, 15, 30..total."""
    spec = spec.strip().lower()
    if spec in ("", "all", "*"):
        return list(range(1, total + 1))
    numbers: list[int] = []
    for part in re.split(r"[,;\s]+", spec):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", part)
        if m and (m[1] or m[2]):
            a, b = int(m[1] or 1), int(m[2] or total)
            numbers.extend(range(a, b + 1) if a <= b else range(a, b - 1, -1))
        elif part.isdigit():
            numbers.append(int(part))
        else:
            raise ValueError(f"'{part}' is not a slide number. Use e.g. 1-10, 15, 30-")
    slides = [n for n in dict.fromkeys(numbers) if 1 <= n <= total]
    if not slides:
        raise ValueError(f"None of '{spec}' is between 1 and {total}.")
    return slides


def compress_ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 7, 9, 10] -> '1-3, 7, 9-10'"""
    parts: list[str] = []
    for n in sorted(numbers):
        if parts and parts[-1][1] == n - 1:
            parts[-1][1] = n
        else:
            parts.append([n, n])
    return ", ".join(f"{a}-{b}" if a != b else f"{a}" for a, b in parts)


def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120].rstrip(" .") or "slideshare"


def output_path(value: str, title: str) -> Path:
    value = value.strip()
    path = Path(value).expanduser() if value else Path(safe_filename(title) + ".pdf")
    if value.endswith(("/", "\\")) or path.is_dir():
        path = path / (safe_filename(title) + ".pdf")
    if path.suffix.lower() != ".pdf":
        path = path.with_name(path.name + ".pdf")
    return path.resolve()


def open_file(path: Path) -> bool:
    for opener in ("xdg-open", "open"):
        exe = shutil.which(opener)
        if exe:
            subprocess.Popen([exe, str(path)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
    return False


# --------------------------------------------------------------------------
# TUI
# --------------------------------------------------------------------------
PALETTE = Theme(                 # a dark keyboard with a red TrackPoint in the middle
    name="trackpoint",
    primary="#E2231A",           # ThinkPad red
    secondary="#5FB3A1",
    accent="#E2231A",
    foreground="#E9E4D8",        # paper
    background="#14171F",
    surface="#1B1F2A",
    panel="#262B38",
    success="#5FB3A1",
    warning="#E5C07B",           # yellow, so a warning never looks like the red accent
    error="#FF6B81",             # lighter and pinker than the accent
    dark=True,
)
LOG_STYLE = {"ok": ("✓", PALETTE.success), "warn": ("!", PALETTE.warning),
             "error": ("✗", PALETTE.error), "info": ("›", "#8C92A3")}


SHORT_ROWS = 30                  # terminals lower than this get the compact layout


class SlideShareApp(App):
    TITLE = "SlideShare → PDF"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #body { padding: 1 2 0 2; }
    #url-row, #options, #actions { height: auto; }
    #url { width: 1fr; }
    #find { margin-left: 1; min-width: 15; }

    #info {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        border: round $primary 60%;
        border-title-color: $primary;
    }
    #info-title { color: $primary; text-style: bold; }
    #info-url { color: $text-muted; }
    #info.error { border: round $error; border-title-color: $error; background: $error 8%; }
    #info { max-height: 12; }                    /* never let one odd page take over the screen */
    Screen.-short #info { max-height: 8; }
    #info.error #info-title { color: $error; }

    #options { margin-top: 1; }
    #options Vertical { height: auto; }
    .label { color: $text-muted; padding: 0 1; }
    #out-col { width: 2fr; }
    #slides-col { width: 1fr; min-width: 14; margin-left: 1; }
    #size-col { width: 16; margin-left: 1; }
    #keep-col { width: auto; margin-left: 1; }

    #actions { margin-top: 1; align: left middle; }
    #actions Button { margin-right: 1; }
    #download { min-width: 16; }
    #stop { min-width: 10; }
    #open { min-width: 12; }
    #progress { width: 1fr; margin-left: 1; }
    #progress Bar { width: 1fr; }
    #count { width: auto; min-width: 7; margin-left: 1; color: $text-muted; }

    #log {
        height: 1fr;
        min-height: 3;
        margin-top: 1;
        padding: 0 1;
        background: $surface;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        overflow-x: hidden;
        overflow-y: scroll;             /* a fixed width, so wrapped lines always fit */
        scrollbar-size-vertical: 1;
        scrollbar-background: $surface;
        scrollbar-background-hover: $surface;
        scrollbar-background-active: $surface;
        scrollbar-color: $panel-lighten-2;
        scrollbar-color-hover: $primary 70%;
        scrollbar-color-active: $primary;
    }

    /* low terminals (e.g. 80x24): no header, no labels, one-line buttons */
    Screen.-short Header { display: none; }
    Screen.-short #body { padding: 0 1; }
    Screen.-short .label { display: none; }
    Screen.-short #actions Button { height: 1; border: none !important; min-width: 0; }
    """
    BINDINGS = [
        Binding("ctrl+s", "download", "Download PDF", priority=True),
        Binding("escape", "stop", "Stop"),
        Binding("ctrl+o", "open_pdf", "Open PDF", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.deck: Optional[Deck] = None
        self.last_pdf: Optional[Path] = None
        self.busy = False
        self.stop_event = threading.Event()
        self._auto_out = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            with Horizontal(id="url-row"):
                yield Input(placeholder="https://www.slideshare.net/slideshow/…", id="url")
                yield Button("Find slides", id="find", variant="primary")
            with Vertical(id="info"):
                yield Static(id="info-title")
                yield Static(id="info-meta")
                yield Static(id="info-url")
            with Horizontal(id="options"):
                with Vertical(id="out-col"):
                    yield Label("Save as", classes="label")
                    yield Input(placeholder="file name or folder", id="out")
                with Vertical(id="slides-col"):
                    yield Label("Slides", classes="label")
                    yield Input(placeholder="all", id="slides")
                with Vertical(id="size-col"):
                    yield Label("Image size", classes="label")
                    yield Select([(f"{w} px", w) for w in SIZES], value=self.args.size,
                                 allow_blank=False, id="size")
                with Vertical(id="keep-col"):
                    yield Label(" ", classes="label")
                    yield Checkbox("Keep images", value=self.args.keep, id="keep")
            with Horizontal(id="actions"):
                yield Button("Download PDF", id="download", variant="success", disabled=True)
                yield Button("Stop", id="stop", variant="error", disabled=True)
                yield Button("Open PDF", id="open", disabled=True)
                yield ProgressBar(total=100, id="progress", show_eta=True)
                yield Label("", id="count")
            yield RichLog(id="log", wrap=True, min_width=40, max_lines=5000)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(PALETTE)
        self.theme = PALETTE.name
        self._fit_height(self.size.height)
        self.query_one("#info").border_title = "Presentation"
        self.query_one("#log").border_title = "Log"
        self.query_one("#out", Input).value = self.args.out
        self.query_one("#out", Input).tooltip = "PDF file name, a folder, or a full path"
        self.query_one("#slides", Input).value = self.args.slides
        self.query_one("#slides", Input).tooltip = "all slides, or e.g. 1-10, 15, 30-"
        self.query_one("#keep", Checkbox).tooltip = "Keep the slide images in a folder next to the PDF"
        self._show_info("Paste a presentation link above and press Enter.",
                        "A slide image address, the embed code or a saved .html page work too.",
                        "Slides: leave empty for all, or type e.g. 1-10, 15, 30-")
        url = self.query_one("#url", Input)
        url.focus()
        if self.args.url:
            url.value = self.args.url
            self._find()

    def on_resize(self, event: events.Resize) -> None:
        self._fit_height(event.size.height)

    def _fit_height(self, rows: int) -> None:
        self.screen.set_class(rows < SHORT_ROWS, "-short")

    # -- small UI helpers --------------------------------------------------
    def _ui(self, fn: Callable, *args) -> None:
        """Run fn on the UI thread (called from worker threads)."""
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            if self.is_running:
                raise

    def _log_from_thread(self, level: str, msg: str) -> None:
        self._ui(self._write_log, level, msg)

    def _write_log(self, level: str, msg: str) -> None:
        icon, color = LOG_STYLE.get(level, LOG_STYLE["info"])
        self.query_one("#log", RichLog).write(Text.assemble((f"{icon} ", f"bold {color}"), msg))

    def _show_info(self, title: str, meta: str = "", url: str = "", error: bool = False) -> None:
        self.query_one("#info").set_class(error, "error")
        for selector, text in (("#info-title", title), ("#info-meta", meta), ("#info-url", url)):
            widget = self.query_one(selector, Static)
            widget.update(Text(text))
            widget.display = bool(text)

    def _show_deck(self) -> None:
        deck = self.deck
        size = int(self.query_one("#size", Select).value)
        meta = f"{deck.total} slides" + (f", uploaded by {deck.author}" if deck.author else "")
        self._show_info(deck.title, meta, deck.url("{n}", size))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.query_one("#find", Button).disabled = busy
        self.query_one("#download", Button).disabled = busy or self.deck is None
        self.query_one("#stop", Button).disabled = not busy
        self.query_one("#open", Button).disabled = busy or self.last_pdf is None
        self.refresh_bindings()

    def _progress(self, done: int, total: int) -> None:
        self.query_one("#progress", ProgressBar).update(total=total, progress=done)
        self.query_one("#count", Label).update(f"{done} / {total}")

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        busy = getattr(self, "busy", False)
        if action == "download":
            return None if busy or getattr(self, "deck", None) is None else True
        if action == "stop":
            return True if busy else None
        if action == "open_pdf":
            return None if busy or getattr(self, "last_pdf", None) is None else True
        return True

    # -- step 1: find the slides -------------------------------------------
    @on(Input.Submitted, "#url")
    @on(Button.Pressed, "#find")
    def _find(self) -> None:
        if self.busy:
            return
        url = self.query_one("#url", Input).value.strip()
        if not url:
            self.notify("Paste a SlideShare link first.", severity="warning")
            return
        self.deck = None
        self.last_pdf = None
        self.stop_event.clear()
        self._set_busy(True)
        self._show_info("Reading the presentation…", url)
        self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        self.query_one("#count", Label).update("")
        self._find_worker(url)

    @work(thread=True, exclusive=True, group="job")
    def _find_worker(self, url: str) -> None:
        try:
            deck = resolve(url, log=self._log_from_thread, stop=self.stop_event,
                           browser=not self.args.no_browser)
        except ResolveError as exc:
            self._ui(self._find_failed, str(exc))
        except Exception as exc:                     # keep the UI usable whatever happens
            self._ui(self._find_failed, f"Unexpected error: {exc!r}")
        else:
            self._ui(self._find_done, deck)

    def _find_done(self, deck: Deck) -> None:
        self.deck = deck
        self.sub_title = deck.title
        self._show_deck()
        out = self.query_one("#out", Input)
        if out.value.strip() in ("", self._auto_out):
            out.value = self._auto_out = safe_filename(deck.title) + ".pdf"
        self.query_one("#progress", ProgressBar).update(total=deck.total, progress=0)
        self.query_one("#count", Label).update(f"0 / {deck.total}")
        self._write_log("ok", f"Found {deck.total} slides: {deck.title}")
        self._set_busy(False)
        self.query_one("#download", Button).focus()

    def _find_failed(self, message: str) -> None:
        self._show_info("Could not read the slides", message, error=True)
        self._write_log("error", message.split(". ")[0].rstrip("."))   # the details are above
        self.query_one("#progress", ProgressBar).update(total=100, progress=0)
        self._set_busy(False)
        self.query_one("#url", Input).focus()

    # -- step 2: download and build the PDF --------------------------------
    @on(Button.Pressed, "#download")
    @on(Input.Submitted, "#out, #slides")
    def _download_pressed(self) -> None:
        self.action_download()

    def action_download(self) -> None:
        if self.busy or self.deck is None:
            return
        deck = self.deck
        try:
            slides = parse_slides(self.query_one("#slides", Input).value, deck.total)
        except ValueError as exc:
            self.notify(str(exc), title="Slides", severity="error")
            return
        out = output_path(self.query_one("#out", Input).value, deck.title)
        size = int(self.query_one("#size", Select).value)
        keep = self.query_one("#keep", Checkbox).value
        self.stop_event.clear()
        self._set_busy(True)
        self._progress(0, len(slides))
        self._write_log("info", f"Downloading {len(slides)} slides at {size} px")
        self._download_worker(deck, slides, size, out, keep)

    @work(thread=True, exclusive=True, group="job")
    def _download_worker(self, deck: Deck, slides: list[int], size: int, out: Path,
                         keep: bool) -> None:
        try:
            result = run_job(deck, slides, size, out, keep, jobs=self.args.jobs,
                             stop=self.stop_event, log=self._log_from_thread,
                             progress=lambda done, total: self._ui(self._progress, done, total))
        except Exception as exc:
            self._ui(self._job_failed, str(exc) or repr(exc))
        else:
            self._ui(self._job_done, result)

    def _job_done(self, result: JobResult) -> None:
        if result.cancelled:
            self._write_log("warn", "Stopped. No PDF was written.")
            self.notify("Stopped. No PDF was written.", severity="warning")
        elif result.pdf is None:
            self._write_log("error", "None of the slides could be downloaded, so no PDF was written.")
            self.notify("None of the slides could be downloaded.", title="No PDF", severity="error")
        else:
            self.last_pdf = result.pdf
            size_mb = result.pdf.stat().st_size / 1_000_000
            self._write_log("ok", f"PDF downloaded: {result.pdf}  "
                                  f"({result.pages} pages, {size_mb:.1f} MB, {result.method})")
            if result.failed:
                self._write_log("warn", f"Missing slides: {compress_ranges(result.failed)}")
            self.notify(f"{result.pages} pages saved to {result.pdf.name}", title="PDF downloaded",
                        severity="warning" if result.failed else "information")
        self._set_busy(False)
        self.query_one("#open" if self.last_pdf else "#download", Button).focus()

    def _job_failed(self, message: str) -> None:
        self._write_log("error", message)
        self.notify(message, title="Download failed", severity="error")
        self._set_busy(False)

    # -- other actions -----------------------------------------------------
    @on(Button.Pressed, "#stop")
    def _stop_pressed(self) -> None:
        self.action_stop()

    def action_stop(self) -> None:
        if self.busy and not self.stop_event.is_set():
            self.stop_event.set()
            self._write_log("warn", "Stopping…")

    @on(Button.Pressed, "#open")
    def _open_pressed(self) -> None:
        self.action_open_pdf()

    def action_open_pdf(self) -> None:
        if self.last_pdf and self.last_pdf.exists() and not open_file(self.last_pdf):
            self.notify(str(self.last_pdf), title="No xdg-open found, the PDF is here")

    @on(Select.Changed, "#size")
    def _size_changed(self) -> None:
        if self.deck is not None:
            self._show_deck()

    async def action_quit(self) -> None:
        self.stop_event.set()
        self.exit()


# --------------------------------------------------------------------------
# Plain console mode
# --------------------------------------------------------------------------
def run_cli(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                               TimeRemainingColumn)

    console = Console()
    colors = {"ok": "green", "warn": "yellow", "error": "red", "info": "dim"}

    def log(level: str, msg: str) -> None:
        console.print(f"[{colors.get(level, 'dim')}]{escape(msg)}[/]")

    def problems_only(level: str, msg: str) -> None:
        if level in ("warn", "error"):
            log(level, msg)

    try:
        deck = resolve(args.url, log=log, browser=not args.no_browser)
        slides = parse_slides(args.slides, deck.total)
    except (ResolveError, ValueError) as exc:
        log("error", str(exc))
        return 1
    out = output_path(args.out, deck.title)
    count = f"{deck.total} slides" + (f", downloading {len(slides)}" if len(slides) != deck.total else "")
    console.print(f"[bold]{escape(deck.title)}[/]  {count} → {escape(str(out))}")
    with Progress(TextColumn("Downloading"), BarColumn(), MofNCompleteColumn(),
                  TimeRemainingColumn(), console=console) as bar:
        task = bar.add_task("", total=len(slides))
        result = run_job(deck, slides, args.size, out, args.keep, jobs=args.jobs,
                         log=problems_only,
                         progress=lambda done, _total: bar.update(task, completed=done))
    if result.pdf is None:
        log("error", "None of the slides could be downloaded, so no PDF was written.")
        return 1
    if result.failed:
        log("warn", f"Missing slides: {compress_ranges(result.failed)}")
    log("ok", f"PDF downloaded: {result.pdf} ({result.pages} pages, {result.method})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a SlideShare presentation as one PDF.")
    parser.add_argument("url", nargs="?", default="",
                        help="presentation link, slide image link, embed code or a saved .html page")
    parser.add_argument("-o", "--out", default="",
                        help="output PDF (default: '<title>.pdf' in the current folder)")
    parser.add_argument("-s", "--size", type=int, choices=SIZES, default=2048,
                        help="slide width in px (default: 2048)")
    parser.add_argument("--slides", default="", help="which slides, e.g. 1-10,15 (default: all)")
    parser.add_argument("-k", "--keep", action="store_true",
                        help="keep the downloaded images next to the PDF")
    parser.add_argument("-j", "--jobs", type=int, default=6, help="parallel downloads (default: 6)")
    parser.add_argument("--no-browser", action="store_true",
                        help="never start a hidden Chrome/Chromium to get past the bot check")
    parser.add_argument("--no-tui", action="store_true", help="plain console output, no TUI")
    args = parser.parse_args()

    if args.no_tui or not sys.stdout.isatty():
        if not args.url:
            parser.error("the console mode needs a link")
        return run_cli(args)
    SlideShareApp(args).run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)