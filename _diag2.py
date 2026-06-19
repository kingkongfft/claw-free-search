"""Find the assistant reply segment for kimi precisely."""

import asyncio
import json
import sys

sys.path.insert(0, "chat-router")
from app import PROVIDERS, PROVIDER_CONFIGS, load_storage, USER_AGENT
from playwright.async_api import async_playwright

PROV = sys.argv[1] if len(sys.argv) > 1 else "kimi"


def p(*a):
    sys.stdout.buffer.write(
        (" ".join(str(x) for x in a) + "\n").encode("utf-8", "replace")
    )
    sys.stdout.flush()


async def main():
    cfg = PROVIDER_CONFIGS[PROV]
    prov = PROVIDERS[PROV]
    st = load_storage(PROV)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 800}, user_agent=USER_AGENT
    )
    if st.get("cookies"):
        await ctx.add_cookies(st["cookies"])
    if st.get("localStorage"):
        await ctx.add_init_script(
            "(() => { const items = "
            + json.dumps(st["localStorage"])
            + "; try { for (const k in items) localStorage.setItem(k, items[k]); } catch(e){} })();"
        )
    page = await ctx.new_page()
    await page.goto(prov["url"], wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(4)
    inp = await page.wait_for_selector('div[contenteditable="true"]', timeout=8000)
    await inp.evaluate("e=>e.focus()")
    await page.keyboard.type("用一句话解释什么是相对论")
    await page.keyboard.press("Enter")
    await asyncio.sleep(12)
    # candidate selectors to test
    sels = [
        '[class*="segment-content"]',
        '[class*="markdown"]',
        '[class*="paragraph"]',
        '[class*="segment-assistant"]',
        '[class*="assistant"]',
        ".chat-content-item",
        '[class*="chat-content-item"]',
        '[class*="response"]',
        '[class*="answer"]',
    ]
    for s in sels:
        txts = await page.evaluate(
            """(s)=>{const a=[];document.querySelectorAll(s).forEach(e=>{const t=(e.innerText||'').trim();if(t)a.push(t.slice(0,80));});return a;}""",
            s,
        )
        p(f"{s}  -> {len(txts)} matches; sample: {txts[:2]!r}")
    await browser.close()
    await pw.stop()


asyncio.run(main())
