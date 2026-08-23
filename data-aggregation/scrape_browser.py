"""Fetch JS-rendered HTML via Playwright (Chrome when available)."""

from __future__ import annotations

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

_CHROMIUM_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
)


def fetch_rendered_html(
    url: str,
    wait_selector: str,
    *,
    timeout_ms: int = 60_000,
    headed: bool = False,
    extra_wait_ms: int = 0,
    optional_selector: str | None = None,
    optional_timeout_ms: int = 15_000,
    require_selector: bool = True,
) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launch_kwargs: dict[str, object] = {
            "headless": not headed,
            "args": list(_CHROMIUM_ARGS),
        }
        try:
            browser = playwright.chromium.launch(channel="chrome", **launch_kwargs)
        except Exception:
            browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_selector(
                    wait_selector, timeout=timeout_ms, state="attached"
                )
            except Exception:
                if require_selector:
                    raise
            if optional_selector:
                try:
                    page.wait_for_selector(optional_selector, timeout=optional_timeout_ms)
                except Exception:
                    pass
            if extra_wait_ms:
                page.wait_for_timeout(extra_wait_ms)
            return page.content()
        finally:
            browser.close()
