import atexit
import time

from tools.guardrails import check_url

_pw = None
_browser = None
_page = None
_current_headless = None  # track mode so we can switch if needed


def _get_page(headless: bool = True):
    global _pw, _browser, _page, _current_headless
    if _page is not None and _current_headless != headless:
        # Mode switch requested — restart browser in the new mode
        _close_browser()

    if _page is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            slow_mo=0 if headless else 80,  # slow_mo helps human observation
        )
        ctx = _browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        _page = ctx.new_page()
        _page.set_default_timeout(15_000)
        _current_headless = headless
        atexit.register(_close_browser)

    return _page


def _close_browser():
    global _pw, _browser, _page, _current_headless
    try:
        if _page:
            _page.close()
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _page = _browser = _pw = _current_headless = None


def _page_snapshot(page) -> str:
    try:
        text = page.inner_text("body")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        snippet = "\n".join(lines[:120])
        return f"URL: {page.url}\n\n{snippet}"
    except Exception:
        return f"URL: {page.url}\n(could not read page text)"


def browser(action: str, url: str = None, text: str = None,
            selector: str = None, value: str = None,
            label: str = None, screenshot_path: str = None,
            headless: bool = True) -> str:
    try:
        page = _get_page(headless=headless)
    except RuntimeError as e:
        return str(e)

    try:
        if action == "navigate":
            blocked = check_url(url) if url else None
            if blocked:
                return blocked
            page.goto(url, wait_until="domcontentloaded")
            if not headless:
                time.sleep(0.5)
            return _page_snapshot(page)

        elif action == "get_text":
            return _page_snapshot(page)

        elif action == "get_url":
            return page.url

        elif action == "click":
            if text:
                page.get_by_text(text, exact=False).first.click()
            elif selector:
                page.click(selector)
            else:
                return "Error: provide 'text' or 'selector' for click"
            if not headless:
                time.sleep(0.3)
            return _page_snapshot(page)

        elif action == "type":
            if label:
                page.get_by_label(label, exact=False).first.fill(value or "")
            elif selector:
                page.fill(selector, value or "")
            else:
                return "Error: provide 'label' or 'selector' for type"
            return "Typed into field"

        elif action == "press":
            page.keyboard.press(value or "Enter")
            if not headless:
                time.sleep(0.3)
            return _page_snapshot(page)

        elif action == "scroll_down":
            page.evaluate("window.scrollBy(0, 600)")
            return "Scrolled down"

        elif action == "scroll_up":
            page.evaluate("window.scrollBy(0, -600)")
            return "Scrolled up"

        elif action == "back":
            page.go_back(wait_until="domcontentloaded")
            return _page_snapshot(page)

        elif action == "wait_for_user":
            # Pause and let the user complete an action in the visible browser.
            # Returns when the page URL or content changes, or after a timeout.
            if headless:
                return "wait_for_user requires headless=false — relaunch with headless=false first."
            prompt = value or "Complete the action in the browser window, then press Enter here…"
            try:
                input(f"\n  [browser] {prompt}\n  > ")
            except (EOFError, KeyboardInterrupt):
                pass
            return _page_snapshot(page)

        elif action == "screenshot":
            path = screenshot_path or f"screenshot_{int(time.time())}.png"
            page.screenshot(path=path, full_page=False)
            return f"Screenshot saved: {path}"

        elif action == "close":
            _close_browser()
            return "Browser closed"

        else:
            return f"Unknown browser action: '{action}'"

    except Exception as e:
        return f"Browser error ({action}): {e}"


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "browser",
            "description": (
                "Control a Chromium browser to navigate pages, click, fill forms, scrape content, and take screenshots.\n\n"
                "HEADLESS MODE (headless=true, default): runs silently in the background. "
                "Use for: web scraping, reading page content, data extraction, checking URLs, "
                "any automated flow where no human interaction is needed.\n\n"
                "VISIBLE MODE (headless=false): opens a real browser window the user can see and interact with. "
                "Use for: CAPTCHA solving, 2FA / MFA authentication, OAuth / SSO login flows, "
                "payment confirmation, sites that block headless browsers (bot protection). "
                "When you hit an auth wall, CAPTCHA, or bot-detection page — switch to headless=false "
                "and use wait_for_user so the user can complete that step before you continue.\n\n"
                "After navigate or click, the page text is returned so you know the current state. "
                "Chain calls to complete multi-step flows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "navigate", "click", "type", "press", "get_text", "get_url",
                            "scroll_down", "scroll_up", "back", "wait_for_user",
                            "screenshot", "close",
                        ],
                        "description": (
                            "navigate: go to URL | click: click element | type: fill input | "
                            "press: keyboard key | get_text: read current page | get_url: current URL | "
                            "scroll_down/up: scroll | back: browser back | "
                            "wait_for_user: pause for user to complete action (visible mode only) | "
                            "screenshot: save PNG | close: shut down browser"
                        ),
                    },
                    "headless": {
                        "type": "boolean",
                        "description": (
                            "true (default): silent background browser. "
                            "false: open visible window — use when CAPTCHA, 2FA, OAuth, payment, "
                            "or bot-detection is encountered."
                        ),
                        "default": True,
                    },
                    "url": {
                        "type": "string",
                        "description": "URL for 'navigate' action",
                    },
                    "text": {
                        "type": "string",
                        "description": "Visible text of element to click",
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to target an element",
                    },
                    "label": {
                        "type": "string",
                        "description": "Label of the input field (for 'type' action)",
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "Text to type (for 'type'), key to press (for 'press', e.g. 'Enter'), "
                            "or message to show user (for 'wait_for_user')"
                        ),
                    },
                    "screenshot_path": {
                        "type": "string",
                        "description": "File path to save screenshot (defaults to screenshot_<timestamp>.png)",
                    },
                },
                "required": ["action"],
            },
        },
    },
]
