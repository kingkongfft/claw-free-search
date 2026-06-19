"""Measure kimi generating-flag and reply growth over time after send."""

import asyncio, json, sys, time

sys.path.insert(0, "chat-router")
from app import PROVIDERS, PROVIDER_CONFIGS, load_storage, USER_AGENT, _fill_input
from playwright.async_api import async_playwright

PROV = "kimi"


def p(*a):
    sys.stdout.buffer.write((" ".join(map(str, a)) + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


async def main():
    cfg = PROVIDER_CONFIGS[PROV]
    prov = PROVIDERS[PROV]
    st = load_storage(PROV)
    pw = await async_playwright().start()
    br = await pw.chromium.launch(headless=True)
    ctx = await br.new_context(
        viewport={"width": 1280, "height": 800}, user_agent=USER_AGENT
    )
    if st.get("cookies"):
        await ctx.add_cookies(st["cookies"])
    if st.get("localStorage"):
        await ctx.add_init_script(
            "(()=>{const i="
            + json.dumps(st["localStorage"])
            + ";try{for(const k in i)localStorage.setItem(k,i[k])}catch(e){}})();"
        )
    page = await ctx.new_page()
    await page.goto(prov["url"], wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(4)
    inp = await page.wait_for_selector('div[contenteditable="true"]', timeout=8000)
    await _fill_input(page, inp, "用一句话解释什么是黑洞")
    await page.keyboard.press("Enter")
    gsel = cfg["generating_sel"]
    t0 = time.time()
    for i in range(30):
        await asyncio.sleep(1.5)
        info = await page.evaluate(
            """(g)=>{
            const seg=document.querySelectorAll('[class*=\"segment-assistant\"]');
            let txt=''; seg.forEach(e=>{const t=(e.innerText||'').trim(); if(t.length>txt.length)txt=t;});
            return {gen: document.querySelectorAll(g).length, len: txt.length, sample: txt.slice(0,40)};
        }""",
            gsel,
        )
        p(
            f"t={time.time() - t0:4.1f}s gen={info['gen']} len={info['len']} :: {info['sample']!r}"
        )
        if info["len"] > 0 and info["gen"] == 0:
            p("  -> would COMPLETE here")
    await br.close()
    await pw.stop()


asyncio.run(main())
