"""How the page looks: the Gradio theme, and the CSS and JS from static/."""
import json
import os
import tempfile
from urllib.parse import quote

import gradio as gr

from .views import ICONS, logo_svg

STATIC = os.path.join(os.path.dirname(__file__), "static")


def read_static(name):
    with open(os.path.join(STATIC, name), "r", encoding="utf-8") as f:
        return f.read()


BRAND_HUE = gr.themes.Color(
    c50="#fff4f1", c100="#ffe6df", c200="#ffcdbf", c300="#ffa892", c400="#ff7b5e", c500="#f9573a",
    c600="#e33d22", c700="#bf2f18", c800="#9d2a19", c900="#82281b", c950="#470f08", name="booktick",
)
GRADIENT = "linear-gradient(135deg, #ff6b4a 0%, #f43f7a 100%)"
GRADIENT_HOVER = "linear-gradient(135deg, #ff5a36 0%, #ec2d6c 100%)"

theme = gr.themes.Soft(
    primary_hue=BRAND_HUE,
    secondary_hue="slate",
    neutral_hue="slate",
    radius_size=gr.themes.sizes.radius_lg,
    font=[gr.themes.GoogleFont("Plus Jakarta Sans"), "ui-sans-serif", "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "Consolas", "monospace"],
).set(
    body_background_fill="#f4f4f9",
    body_background_fill_dark="#0b0c16",
    body_text_color="#161729",
    body_text_color_dark="#ececf4",
    body_text_color_subdued="#686a82",
    body_text_color_subdued_dark="#9a9cb5",
    background_fill_primary="#ffffff",
    background_fill_primary_dark="#151624",
    background_fill_secondary="#f7f7fb",
    background_fill_secondary_dark="#1c1d2e",
    border_color_primary="#e8e8f0",
    border_color_primary_dark="#2a2b40",
    block_background_fill="#ffffff",
    block_background_fill_dark="#151624",
    block_border_color="#e8e8f0",
    block_border_color_dark="#2a2b40",
    block_shadow="none",
    block_shadow_dark="none",
    block_label_text_weight="600",
    block_title_text_weight="650",
    block_title_background_fill="transparent",
    block_title_background_fill_dark="transparent",
    block_title_text_color="#161729",
    block_title_text_color_dark="#ececf4",
    block_title_padding="0",
    block_title_radius="0",
    block_info_text_color="#686a82",
    block_info_text_color_dark="#9a9cb5",
    input_background_fill="#f7f7fb",
    input_background_fill_dark="#1c1d2e",
    # A crisp 1px edge on every input, so focus reads as a sharp brand outline with a faint
    # halo instead of a soft blur (the Soft theme has no input border at all).
    input_border_width="1px",
    input_border_width_dark="1px",
    input_border_color="#e3e3ec",
    input_border_color_dark="#2c2d44",
    input_border_color_hover="#cdcedc",
    input_border_color_hover_dark="#3d3e5a",
    input_border_color_focus="#f9573a",
    input_border_color_focus_dark="#ff6b4a",
    input_shadow="none",
    input_shadow_dark="none",
    input_shadow_focus="0 0 0 3px rgba(249, 87, 58, .12)",
    input_shadow_focus_dark="0 0 0 3px rgba(255, 107, 74, .16)",
    button_large_radius="12px",
    button_small_radius="10px",
    button_primary_background_fill=GRADIENT,
    button_primary_background_fill_dark=GRADIENT,
    button_primary_background_fill_hover=GRADIENT_HOVER,
    button_primary_background_fill_hover_dark=GRADIENT_HOVER,
    button_primary_text_color="#ffffff",
    button_primary_text_color_dark="#ffffff",
    button_primary_border_color="transparent",
    button_primary_border_color_dark="transparent",
    button_secondary_background_fill="#ffffff",
    button_secondary_background_fill_dark="#1c1d2e",
    button_secondary_background_fill_hover="#f4f4f9",
    button_secondary_background_fill_hover_dark="#24253a",
    button_secondary_border_color="#e3e3ec",
    button_secondary_border_color_dark="#30314a",
    button_secondary_text_color="#161729",
    button_secondary_text_color_dark="#ececf4",
    slider_color="#f9573a",
    slider_color_dark="#f9573a",
    color_accent_soft="#ffe6df",
    color_accent_soft_dark="rgba(249, 87, 58, .16)",
)

CSS = read_static("app.css")

FAVICON_SVG = logo_svg("fav", "bt-favicon")  # the same mark as the header logo
FAVICON = "data:image/svg+xml," + quote(FAVICON_SVG)
# The page script draws the delete dialog's icon; everything else it needs is in app.js.
ICON_SVGS = {"trash": f'<svg class="bt-i" viewBox="0 0 24 24" aria-hidden="true">{ICONS["trash"]}</svg>'}

HEAD = "\n".join([
    f'<link rel="icon" type="image/svg+xml" href="{FAVICON}">',
    '<meta name="theme-color" content="#12132a">',
    f"<script>window.BT_ICONS = {json.dumps(ICON_SVGS)};</script>",
    "<script>",
    read_static("app.js"),
    "</script>",
])


def favicon_file():
    """Write the favicon to disk so Gradio serves it at /favicon.ico instead of its own logo.

    Browsers ask for /favicon.ico before the page script adds the <link> in HEAD, and
    bookmarks and link previews only ever use that address.
    """
    path = os.path.join(tempfile.gettempdir(), "booktick-favicon.svg")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(FAVICON_SVG)
        return path
    except OSError:
        return None
