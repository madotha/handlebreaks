"""Render the logo files: static/favicon.svg and the app icons in static/icons/.

The logo is a film frame with two chevrons (amber on dark, or dark on amber). The header uses the
same shapes as the symbol "logo" in templates/_icons.html; change both together.

Needs Playwright with Chromium (pip install playwright && playwright install chromium); only run it
when the logo changes:

    python3 docs/make_icons.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parent.parent / "static"
AMBER, AMBER_TOP, AMBER_BOTTOM = "#f2b632", "#f2b632", "#e09a06"
DARK_TOP, DARK_BOTTOM, INK = "#2c2e33", "#17181b", "#1a1406"

GLYPH = ('<rect x="5.5" y="8.5" width="21" height="15" rx="3" stroke-width="2"/>'
         '<path d="M9 8.5v-2M13 8.5v-2M17 8.5v-2M21 8.5v-2M9 25.5v-2M13 25.5v-2M17 25.5v-2M21 25.5v-2" stroke-width="1.6" opacity=".7"/>'
         '<path d="M11.5 12 15.5 16l-4 4M16.5 13 19.5 16l-3 3" stroke-width="2.2"/>')


def glyph(color, scale=1.0):
    t = f' transform="translate(16 16) scale({scale}) translate(-16 -16)"' if scale != 1 else ""
    return (f'<g fill="none" stroke="{color}" stroke-linecap="round" stroke-linejoin="round"{t}>{GLYPH}</g>')


def icon(dark=True, rounded=True, scale=1.0, background=True, color=None):
    top, bottom = (DARK_TOP, DARK_BOTTOM) if dark else (AMBER_TOP, AMBER_BOTTOM)
    rx = ' rx="7.5"' if rounded else ""
    tile = (f'<defs><linearGradient id="t" x2="0" y2="1"><stop stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/>'
            f'</linearGradient></defs><rect width="32" height="32"{rx} fill="url(#t)"/>') if background else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">{tile}'
            f'{glyph(color or (AMBER if dark else INK), scale)}</svg>')


# Browser tab: the light variant, the dark one when the system is dark
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><style>'
           f'.a{{stop-color:{AMBER_TOP}}}.b{{stop-color:{AMBER_BOTTOM}}}.g{{stroke:{INK}}}'
           f'@media (prefers-color-scheme:dark){{.a{{stop-color:{DARK_TOP}}}.b{{stop-color:{DARK_BOTTOM}}}.g{{stroke:{AMBER}}}}}</style>'
           '<defs><linearGradient id="t" x2="0" y2="1"><stop class="a"/><stop class="b" offset="1"/></linearGradient></defs>'
           '<rect width="32" height="32" rx="7.5" fill="url(#t)"/>'
           f'<g class="g" fill="none" stroke-linecap="round" stroke-linejoin="round">{GLYPH}</g></svg>')

# Home screens get the dark variant: iOS darkens web app icons itself in dark mode, which turns
# a dark symbol on amber almost black; amber on dark stays readable in both modes.
PNGS = [
    ("icons/icon-192.png", 192, icon(), True),
    ("icons/icon-512.png", 512, icon(), True),
    ("icons/icon-maskable-512.png", 512, icon(rounded=False, scale=.8), False),     # safe zone: 80 % circle
    ("icons/apple-touch-icon.png", 180, icon(rounded=False, scale=.95), False),     # iOS rounds the corners
    ("icons/icon-monochrome-512.png", 512, icon(background=False, scale=.8, color="#ffffff"), True),  # Android themed icons
]


def main():
    (STATIC / "favicon.svg").write_text(FAVICON + "\n")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, size, svg, transparent in PNGS:
            page = browser.new_page(viewport={"width": size, "height": size})
            svg = svg.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
            page.set_content(f'<html><body style="margin:0;background:transparent">{svg}</body></html>')
            page.screenshot(path=str(STATIC / name), omit_background=transparent)
            page.close()
        browser.close()
    print("wrote favicon.svg and", ", ".join(n for n, *_ in PNGS))


if __name__ == "__main__":
    main()
