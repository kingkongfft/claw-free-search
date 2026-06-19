"""Temp diagnostic v2: discover the real reply container classes."""

import asyncio
import json
import sys

sys.path.insert(0, "chat-router")
from app import PROVIDERS, PROVIDER_CONFIGS, load_storage, USER_AGENT
from playwright.async_api import async_playwright

PROV = sys.argv[1] if len(sys.argv) > 1 else "kimi"


def p(*a):
    s = " ".join(str(x) for x in a)
    sys.stdout.buffer.write((s + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


async def main():
    cfg = PROVIDER_CONFIGS[PROV]
    prov = PROVIDERS[PROV]
    st = load_storage(PROV)
    cookies = st.get("cookies", [])
    ls = st.get("localStorage", {})

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 800}, user_agent=USER_AGENT
    )
    if cookies:
        await ctx.add_cookies(cookies)
    if ls:
        await ctx.add_init_script(
            "(() => { const items = "
            + json.dumps(ls)
            + "; try { for (const k in items) localStorage.setItem(k, items[k]); } catch(e){} })();"
        )
    page = await ctx.new_page()
    await page.goto(prov["url"], wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(4)

    inp = None
    for sel in cfg["input_selectors"]:
        try:
            el = await page.wait_for_selector(sel, timeout=4000)
            if el and await el.is_visible():
                inp = el
                break
        except Exception:
            continue
    if not inp:
        p("NO INPUT")
        return
    await inp.evaluate("e=>e.focus()")
    await page.keyboard.type("用一句话解释什么是黑洞")
    await page.keyboard.press("Enter")

    await asyncio.sleep(12)
    dump = await page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('*').forEach(el => {
            const t = (el.innerText || '').trim();
            if (t.length < 40 || t.length > 600) return;
            const cls = el.className && el.className.toString ? el.className.toString() : '';
            out.push({ tag: el.tagName, cls: cls.slice(0,80),
                       dm: el.getAttribute('data-mid')||el.getAttribute('data-message-id')||'',
                       len: t.length, txt: t.slice(0,60) });
        });
        const seen = new Set(); const res=[];
        for (const o of out) { const k=o.cls+'|'+o.len; if(seen.has(k))continue; seen.add(k); res.push(o);}
        return res.slice(0, 40);
    }""")
    for o in dump:
        p(
            f"{o['tag']:6} len={o['len']:4} cls={o['cls']!r} dm={o['dm']!r} :: {o['txt']!r}"
        )

    await browser.close()
    await pw.stop()


asyncio.run(main())
