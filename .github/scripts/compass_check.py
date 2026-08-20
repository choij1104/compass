#!/usr/bin/env python3
"""Compass — release checks.

Compass is used by people who may be watched. The checks below protect the
promises the app makes: nothing leaves the device, the crisis numbers are
always reachable, the lock works, erasing really erases, and the app opens
with no signal.

Run from the repository root:  python3 .github/scripts/compass_check.py
Exit code 1 if any check fails.
"""
import functools
import http.server
import pathlib
import re
import socketserver
import sys
import threading

PORT = 8123
SCREENS = ['s-home', 's-journal', 's-learn', 's-fear', 's-report', 's-recover', 's-settings']
CRISIS = {"988": "Veterans/Military Crisis Line",
          "838255": "crisis text line",
          "877-995-5247": "DoD Safe Helpline",
          "911": "emergency"}
FAILS, NOTES = [], []


def read(p):
    try:
        return open(p, encoding="utf-8").read()
    except FileNotFoundError:
        FAILS.append(f"{p} is missing from the repository")
        return ""


# ---------- static checks ----------
def static_checks(index, sw, licence):
    for num, what in CRISIS.items():
        if num not in index:
            FAILS.append(f"{what} ({num}) no longer appears in the app — the licence "
                         f"forbids removing crisis-resource information")
    if not [n for n in CRISIS if n not in index]:
        NOTES.append("all four crisis contacts present")

    ext = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', index)
    ext = [u for u in ext if 'weather.gov' not in u]   # quick-exit destination
    if ext:
        FAILS.append("external resource referenced: " + ", ".join(sorted(set(ext))))
    net = re.findall(r'\b(fetch\(|XMLHttpRequest|sendBeacon|new WebSocket|EventSource)', index)
    net = [n for n in net if n != 'fetch(']   # fetch( is legitimate inside sw.js only
    if 'fetch(' in index:
        FAILS.append("index.html contains fetch( — Compass must make no network calls")
    if net:
        FAILS.append("network API used in index.html: " + ", ".join(sorted(set(net))))
    if not ext and 'fetch(' not in index and not net:
        NOTES.append("no network calls and no external resources in index.html")

    app = re.search(r"APP_VERSION\s*=\s*'([^']+)'", index)
    cache = re.search(r"CACHE\s*=\s*'compass-v([0-9.]+)'", sw)
    if not app:
        FAILS.append("APP_VERSION not found in index.html")
    if not cache:
        FAILS.append("CACHE in sw.js is not named 'compass-v…'")
    if app and cache:
        if app.group(1) != cache.group(1):
            FAILS.append(f"version stamps disagree — APP_VERSION={app.group(1)}, "
                         f"sw.js cache={cache.group(1)}; devices would keep the old app")
        else:
            NOTES.append(f"version {app.group(1)} consistent across index.html and sw.js")

    for token, what in [("quickExit", "the quick-exit button"),
                        ("submitPin", "the PIN lock"),
                        ("wipeAll", "erase everything"),
                        ("exportData", "export")]:
        if token not in index:
            FAILS.append(f"{what} is gone from the app")

    if "PROPRIETARY" not in licence.upper():
        FAILS.append("LICENSE no longer reads as proprietary — confirm this is intended")
    if "not legal advice" not in index.lower():
        FAILS.append("the 'not legal advice' notice is missing from the app")


# ---------- runtime checks ----------
def runtime_checks():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        FAILS.append("playwright is not installed")
        return

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(pathlib.Path('.').resolve()))
    handler.log_message = lambda *a, **k: None

    class Server(socketserver.TCPServer):
        allow_reuse_address = True

    srv = Server(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{PORT}/"

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={'width': 390, 'height': 844})
        pg = ctx.new_page()
        errs, bad, outside = [], [], []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.on("request", lambda r: outside.append(r.url)
              if not r.url.startswith(base) and not r.url.startswith("data:") else None)

        pg.goto(base); pg.wait_for_timeout(900)

        def keypad(pin="1234", ok=True):
            for d in pin:
                pg.click(f"//button[@class='key' and text()='{d}']")
            if ok:
                pg.click("//button[contains(@class,'key') and text()='OK']")
            pg.wait_for_timeout(450)

        keypad(); keypad()          # set, then confirm
        pg.wait_for_timeout(500)
        if not pg.evaluate("() => document.getElementById('app').style.display !== 'none'"):
            FAILS.append("the app does not open after a PIN is set")
            b.close(); srv.shutdown(); return
        NOTES.append("PIN can be set and opens the app")

        sw = pg.evaluate("""async () => { const r = await navigator.serviceWorker.getRegistration();
            if (!r) return false; await navigator.serviceWorker.ready; return !!r.active; }""")
        if not sw:
            FAILS.append("the service worker did not register — the app will not open offline")
        pg.wait_for_timeout(1200)

        for s in SCREENS:
            pg.evaluate(f"show('{s}')"); pg.wait_for_timeout(180)
            if not pg.evaluate(f"() => document.getElementById('{s}').classList.contains('active')"):
                FAILS.append(f"screen {s} does not render")
        NOTES.append(f"all {len(SCREENS)} screens render")

        seen = set()
        for s in SCREENS:
            pg.evaluate(f"show('{s}')"); pg.wait_for_timeout(180)
            txt = pg.evaluate("() => document.body.innerText")
            for num in CRISIS:
                if num in txt:
                    seen.add(num)
        missing = [n for n in CRISIS if n not in seen]
        if missing:
            FAILS.append("crisis contacts not reachable in the interface: " + ", ".join(missing))
        else:
            NOTES.append("all four crisis contacts reachable on screen")

        pg.evaluate("""() => { db.entries.push({id:'t1', at:Date.now(), what:'check',
            felt:'', who:'', wit:'', ev:''}); save(); }""")
        pg.reload(); pg.wait_for_timeout(700); keypad(ok=False)
        if pg.evaluate("() => db.entries.length") != 1:
            FAILS.append("a saved record did not survive a reload")
        else:
            NOTES.append("records survive a reload")

        ctx.set_offline(True)
        try:
            pg.reload(wait_until="load", timeout=15000); pg.wait_for_timeout(700)
            if not pg.evaluate("() => document.getElementById('lock').classList.contains('active')"):
                FAILS.append("the app does not open with the network off")
            else:
                keypad(ok=False)
                if pg.evaluate("() => db.entries.length") != 1:
                    FAILS.append("records cannot be read offline")
                else:
                    NOTES.append("app opens and records are readable with no network")
        except Exception as ex:
            FAILS.append(f"offline reload failed: {ex}")
        ctx.set_offline(False)

        pg.reload(); pg.wait_for_timeout(700); keypad(ok=False)
        pg.evaluate("confirmWipe()"); pg.wait_for_timeout(1500)
        left = pg.evaluate("""async () => ({ data: localStorage.length,
            caches: (await caches.keys()).length,
            workers: (await navigator.serviceWorker.getRegistrations()).length })""")
        if left['data'] or left['caches'] or left['workers']:
            FAILS.append(f"'erase everything' left something behind: {left}")
        else:
            NOTES.append("erase everything clears records, cached app and worker")

        if outside:
            FAILS.append("the app contacted an outside address: " + ", ".join(sorted(set(outside))[:5]))
        else:
            NOTES.append("no outbound requests during the whole session")
        if bad:
            FAILS.append("HTTP errors: " + ", ".join(sorted(set(bad))))
        if errs:
            FAILS.append("console errors: " + "; ".join(sorted(set(errs))[:5]))
        b.close()
    srv.shutdown()


static_checks(read("index.html"), read("sw.js"), read("LICENSE"))
if not FAILS:
    runtime_checks()
else:
    NOTES.append("(runtime checks skipped — fix the static failures first)")

for n in NOTES:
    print(f"  ok   {n}")
if FAILS:
    print()
    for f in FAILS:
        print(f"  FAIL {f}")
    print(f"\n{len(FAILS)} check(s) failed.")
    sys.exit(1)
print("\nAll checks passed.")
