"""Image generation and HTML-to-image tools.

generate_image — AI image via Pollinations.ai (free) or DALL-E.
html_to_image  — Render an HTML file or inline HTML to a PNG using Playwright.
"""

import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from tools.guardrails import check_url
from tools.state import get_cwd

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "html_to_image",
            "description": (
                "Render an HTML file (or raw HTML string) to a PNG image using a headless browser. "
                "Use this whenever you have created an HTML card, poster, banner, invitation, chart, "
                "or any visual design and the user wants an image. "
                "Returns the saved PNG file path. "
                "Preferred over leaving the user with just an HTML file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Either an absolute/relative file path to an existing .html file, "
                            "or a raw HTML string starting with '<'. "
                            "File paths are resolved relative to the current working directory."
                        ),
                    },
                    "output_filename": {
                        "type": "string",
                        "description": "Output PNG filename without extension (default: 'screenshot').",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Viewport width in pixels (default: 1200).",
                    },
                    "height": {
                        "type": "integer",
                        "description": (
                            "Viewport height in pixels. Pass 0 (default) to auto-fit "
                            "the full page height."
                        ),
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page (default: true).",
                    },
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image from a text prompt using AI and save it to the working directory. "
                "Uses Pollinations.ai (free, no key) by default; uses OpenAI DALL-E when "
                "OPENAI_API_KEY is set. Returns the saved file path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to generate.",
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Output filename without extension, e.g. 'diwali-card'. "
                            "Defaults to 'generated-image'."
                        ),
                    },
                    "width": {
                        "type": "integer",
                        "description": "Image width in pixels (default 1024, max 2048).",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Image height in pixels (default 1024, max 2048).",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Pollinations model to use when not using DALL-E. "
                            "Options: 'flux' (photorealistic), 'flux-realism', 'turbo' (fast), "
                            "'flux-anime', 'flux-3d'. Default: 'flux'."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
    }
]


def _pollinations(prompt: str, filename: str, width: int, height: int, model: str) -> str:
    encoded = urllib.parse.quote(prompt)
    seed = int(time.time()) % 99999
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&model={model}&seed={seed}&nologo=true&enhance=true"
    )

    guard = check_url(url)
    if guard:
        return f"Error: {guard}"

    cwd = get_cwd()
    out_path = Path(cwd) / f"{filename}.png"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "viper-agent/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            out_path.write_bytes(resp.read())
        return f"Image saved: {out_path}  (model: {model}, {width}×{height})"
    except Exception as e:
        return f"Pollinations error: {e}"


def _dalle(prompt: str, filename: str, width: int, height: int) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        return "openai package not installed — run: pip install openai"

    # Snap to DALL-E 3 supported sizes
    size = "1024x1024"
    if width == 1792 or height == 1024:
        size = "1792x1024"
    elif width == 1024 or height == 1792:
        size = "1024x1792"

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    try:
        resp = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size=size,          # type: ignore[arg-type]
            quality="standard",
            n=1,
        )
        image_url = resp.data[0].url
        if not image_url:
            return "DALL-E returned no URL"

        guard = check_url(image_url)
        if guard:
            return f"Error: {guard}"

        cwd = get_cwd()
        out_path = Path(cwd) / f"{filename}.png"
        req = urllib.request.Request(image_url, headers={"User-Agent": "viper-agent/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out_path.write_bytes(r.read())
        return f"Image saved: {out_path}  (DALL-E 3, {size})"
    except Exception as e:
        return f"DALL-E error: {e}"


def html_to_image(
    source: str,
    output_filename: str = "screenshot",
    width: int = 1200,
    height: int = 0,
    full_page: bool = True,
) -> str:
    """Render HTML (file path or raw string) to a PNG via Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "playwright not installed — run: pip install playwright && playwright install chromium"

    cwd = get_cwd()
    out_path = Path(cwd) / f"{output_filename.strip().replace(' ', '-') or 'screenshot'}.png"

    # Determine if source is a file path or raw HTML
    is_raw_html = source.strip().startswith("<")
    if is_raw_html:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
        tmp.write(source)
        tmp.close()
        file_url = Path(tmp.name).as_uri()
        tmp_path = tmp.name
    else:
        html_path = Path(source) if Path(source).is_absolute() else Path(cwd) / source
        if not html_path.exists():
            return f"Error: HTML file not found: {html_path}"
        file_url = html_path.as_uri()
        tmp_path = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            viewport_height = height if height > 0 else 800
            page = browser.new_page(viewport={"width": width, "height": viewport_height})
            page.goto(file_url, wait_until="networkidle")
            # Wait for any CSS animations / fonts
            page.wait_for_timeout(500)
            page.screenshot(path=str(out_path), full_page=full_page)
            browser.close()
        return f"Image saved: {out_path}  ({width}px wide, full_page={full_page})"
    except Exception as e:
        return f"html_to_image error: {e}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def generate_image(
    prompt: str,
    filename: str = "generated-image",
    width: int = 1024,
    height: int = 1024,
    model: str = "flux",
) -> str:
    width = max(256, min(width, 2048))
    height = max(256, min(height, 2048))
    filename = filename.strip().replace(" ", "-") or "generated-image"

    if os.environ.get("OPENAI_API_KEY"):
        return _dalle(prompt, filename, width, height)
    return _pollinations(prompt, filename, width, height, model)
