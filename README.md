# slideshare2pdf

Download a SlideShare presentation as a single PDF — from a terminal UI or the command line.

Paste a presentation link, the script finds the slide images the SlideShare viewer serves from
`image.slidesharecdn.com`, downloads them in parallel and merges them into one PDF.

![slideshare2pdf TUI](https://github.com/user-attachments/assets/df4752e9-0b63-4a28-b5a5-a6da0d984e5a)

## Features

- **Terminal UI** built with [Textual](https://textual.textualize.io/) — paste a link, pick options, watch the progress bar.
- **Headless CLI mode** (`--no-tui`) for scripting and pipelines.
- **Parallel downloads** with retries and per-slide fallbacks.
- **Lossless PDF** via [`img2pdf`](https://pypi.org/project/img2pdf/) when available (the original JPGs are embedded untouched); falls back to Pillow re-encoding otherwise.
- **Automatic slide counting** — uses the page's own slide count when it agrees with the images, otherwise probes the CDN with a galloping binary search.
- **Bot-check fallback** — if the page is behind a challenge, the embed player and the oEmbed API are tried instead.
- **Single slide image URL works too** — copy any `image.slidesharecdn.com/...` address and the whole deck is reconstructed from it.
- **Slide ranges** — `1-10, 15, 30-`.
- **Selectable image width** — 2048 / 1024 / 638 / 320 px.
- **PDF metadata** — title, author and source URL are written into the file.
- Consistent page geometry: every page is 960 pt wide (a 16:9 PowerPoint slide).

---

## Requirements

- Python **3.9+**
- `requests`, `pillow`, `textual >= 1.0`
- `img2pdf` (optional, but recommended — lossless output)

---

## Installation

### Option 1 — `uv` (easiest, no virtualenv to manage)

The script carries [PEP 723](https://peps.python.org/pep-0723/) inline metadata, so `uv` installs the
dependencies for you:

```bash
uv run slideshare2pdf.py
```

### Option 2 — `pipx` (installs it as a normal command)

```bash
pipx install git+https://github.com/LuCaSkooo1/slideshare2pdf.git
slideshare2pdf
```

### Option 3 — plain virtualenv

```bash
python3 -m venv ~/.venvs/slideshare
~/.venvs/slideshare/bin/pip install requests pillow "textual>=1.0" img2pdf
~/.venvs/slideshare/bin/python slideshare2pdf.py
```

Optional: drop it on your `PATH` as a standalone script.

```bash
chmod +x slideshare2pdf.py
sudo install -m 755 slideshare2pdf.py /usr/local/bin/slideshare2pdf
```

---

## Usage

```bash
slideshare2pdf                    # open the TUI
slideshare2pdf LINK               # open the TUI and start resolving right away
slideshare2pdf LINK --no-tui -o deck.pdf --size 2048 --slides 1-10
```

Example:

```bash
slideshare2pdf https://www.slideshare.net/slideshow/netflix-casestudyfina-lv2/24397003 \
  --no-tui -o netflix.pdf
```

### Options

| Flag | Default | Description |
|---|---|---|
| `url` | — | Presentation link, or the address of a single slide image |
| `-o`, `--out` | `<title>.pdf` | Output PDF; a directory is also accepted |
| `-s`, `--size` | `2048` | Slide width in px: `2048`, `1024`, `638`, `320` |
| `--slides` | all | Which slides, e.g. `1-10,15,30-` |
| `-k`, `--keep` | off | Keep the downloaded images next to the PDF |
| `-j`, `--jobs` | `6` | Parallel downloads |
| `--no-tui` | off | Plain console output instead of the TUI |

The console mode is also selected automatically when stdout is not a TTY (piping, CI, cron).

### Keyboard shortcuts (TUI)

| Key | Action |
|---|---|
| `Enter` (URL field) | Find slides |
| `Ctrl+S` | Download PDF |
| `Esc` | Stop the current job |
| `Ctrl+O` | Open the finished PDF |
| `Ctrl+Q` | Quit |

The layout switches to a compact mode on terminals shorter than 30 rows, so 80×24 works fine.

---

## How it works

1. **Resolve** — the presentation page is fetched and scanned for slide image URLs of the shape
   `image.slidesharecdn.com/<deck-key>/<quality>/<slug>-<n>-<width>.jpg`. Since a page also
   advertises other decks, the deck named by the `og:image` thumbnail wins; otherwise the one with
   the most slides on the page.
2. **Fallbacks** — on a bot check or a page without images, the embed player and then the oEmbed API
   (`slide_image_baseurl` + suffix) are tried.
3. **Count** — the page's `totalSlides` value is trusted only if it matches what the images show.
   Otherwise the CDN is probed: gallop upwards (1, 2, 4, 8 …) until a slide 404s, then binary search.
4. **Download** — a thread pool fetches each slide, retrying on 429/5xx and falling back to other
   widths and quality segments when the requested variant is missing.
5. **Build** — `img2pdf` embeds the JPGs losslessly with a fixed 960 pt page width. If that fails
   (e.g. WebP source), Pillow rescales everything to one common width and writes the PDF.

---

## Troubleshooting

**"SlideShare showed a bot check instead of the presentation"**
Wait a minute and retry. If it persists, open the deck in a browser, right-click a slide,
choose *Copy image address* and paste that URL — the whole deck is rebuilt from a single image URL.

**"No slide images found on this page"**
Same workaround: paste a slide image address instead of the page link.

**"The CDN does not answer"**
Transient CDN/network issue. Retry, or lower `--jobs` if you suspect rate limiting.

**Missing slides in the output**
The log lists them as a compressed range (e.g. `12-14, 27`). Some decks genuinely lack certain
widths — try `--size 1024` or `--size 638`.

**`Missing or outdated Python package`**
Your `textual` is older than 1.0 or a dependency is absent. Reinstall with
`pip install -U requests pillow "textual>=1.0" img2pdf`.

---

## Legal notice

This tool only requests images that SlideShare's own public viewer already serves to your browser.
You are responsible for how you use it: respect the copyright of the presentation authors and
SlideShare's Terms of Service, and do not redistribute material you do not have the right to share.
Intended for personal, offline reading of publicly accessible decks.

---

## Contributing

Issues and pull requests are welcome. The whole tool is one dependency-light file, so keep changes
self-contained and avoid adding heavy requirements. Please run it against at least one real
presentation before opening a PR.

## License

MIT — see [LICENSE](LICENSE).
