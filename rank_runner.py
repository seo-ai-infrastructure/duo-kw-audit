#!/usr/bin/env python3
"""
rank_runner.py — ground-truth local rank auditor.

Flow:  Twenty (localsearchtasks)  ->  DuoPlus cloud phone  ->  SERP capture  ->  Twenty
Designed to run on the Mac mini under launchd/cron. No MCP dependency; talks to
Twenty's REST API and DuoPlus's HTTP API directly with API keys.

Built against the REAL localsearchtasks schema:
    keyword       TEXT
    targetCoords  TEXT   "lat,lon"  e.g. "26.1224,-80.1373"
    targetDomain  TEXT   bare domain, e.g. "houseacrepair.com"
    apiRank       NUMBER (you populate from DataForSEO elsewhere)
    realRank      NUMBER (this script writes)
    screenshotUrl LINKS  (composite — see _links())
    auditStatus   MULTI_SELECT

Plus the recommended added fields (see the message accompanying this file):
    deviceId, proxyIpUsed, lastCheckedAt, active,
    inMapPack, mapPackPosition, aiOverviewPresent, citedInAio, rawResult

If you haven't added those yet, the writes for them are isolated in
update_task() — comment out the keys you don't have and the core
realRank/screenshot/status path still works.

================  WHAT'S CONFIRMED vs WHAT YOU MUST VERIFY  ================
CONFIRMED (read live from your workspace / DuoPlus public docs):
  - Twenty object + field names/types above.
  - DuoPlus exposes, by name: Cloud Phone List, Batch Power On, Cloud Phone
    Status, Batch Modify Parameters, Proxy Initialization, Execute the ADB
    command, and the advanced "Dump UIAutomator XML" command. Proxies are
    SOCKS5-only and GPS auto-syncs from the bound proxy's IP geolocation.
  - DuoPlus transport (from API intro): base https://openapi.duoplus.net,
    auth header "DuoPlus-API-Key", EVERY call is POST with a {code,data,message}
    envelope (code==200 = OK). Handled centrally in _duo() below.

STILL MUST VERIFY per endpoint page (marked  # CONFIRM):
  - The exact PATH and request BODY field names for each endpoint, and where
    the value lives inside the response `data`.
  - Whether Batch Modify Parameters accepts raw lat/lng. If not, location is
    proxy-derived and you pin it by assigning a metro-correct SOCKS5 proxy.
===========================================================================
"""

import os
import re
import time
import json
import html
import datetime as dt
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests

# ----------------------------------------------------------------------------
# Config (env)
# ----------------------------------------------------------------------------
TWENTY_BASE   = os.environ.get("TWENTY_BASE", "https://ai-infrastructure.twenty.com/rest")
TWENTY_TOKEN  = os.environ["TWENTY_TOKEN"]               # Settings > APIs in Twenty

DUOPLUS_BASE  = os.environ.get("DUOPLUS_BASE", "https://openapi.duoplus.net")
DUOPLUS_KEY   = os.environ["DUOPLUS_TOKEN"]             # console: Automation -> API menu

STALE_HOURS   = int(os.environ.get("STALE_HOURS", "20"))   # re-audit if older than this
TOP_N         = int(os.environ.get("TOP_N", "20"))         # how deep to scan organic
CHROME_PKG    = "com.android.chrome"
GL, HL        = "us", "en"                                  # country / language for google

S_T = requests.Session(); S_T.headers.update({"Authorization": f"Bearer {TWENTY_TOKEN}"})
S_D = requests.Session()
S_D.headers.update({"DuoPlus-API-Key": DUOPLUS_KEY, "Content-Type": "application/json", "Lang": "en"})


def _duo(path, payload=None):
    """All DuoPlus calls are POST and return {code, data, message}; code==200 = OK.
    Auth header / method / envelope are CONFIRMED from DuoPlus's API intro.
    Per-endpoint path + body field names are still CONFIRM (from each endpoint page)."""
    r = S_D.post(f"{DUOPLUS_BASE}{path}", json=payload or {})
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 200:
        raise RuntimeError(f"DuoPlus {path} -> code {j.get('code')}: {j.get('message')}")
    return j.get("data")


# ----------------------------------------------------------------------------
# Twenty client
# ----------------------------------------------------------------------------
def get_due_tasks():
    """Pull active tasks and decide 'due' in Python (avoids version-specific
    REST filter syntax). N is small, so this is fine."""
    # depth=0 keeps the payload to scalar fields only.
    r = S_T.get(f"{TWENTY_BASE}/localsearchtasks",
                params={"limit": 200, "depth": 0})
    r.raise_for_status()
    rows = r.json().get("data", {}).get("localsearchtasks", [])
    now = dt.datetime.now(dt.timezone.utc)
    due = []
    for t in rows:
        if t.get("active") is False:                     # field may not exist yet -> None -> included
            continue
        last = t.get("lastCheckedAt")
        if last:
            age = now - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
            if age < dt.timedelta(hours=STALE_HOURS):
                continue
        if not t.get("keyword") or not t.get("targetCoords"):
            continue
        due.append(t)
    return due


def _links(url, label="SERP"):
    """Twenty LINKS composite shape. screenshotUrl is a LINKS field, so a bare
    string will be rejected."""
    return {"primaryLinkUrl": url, "primaryLinkLabel": label, "secondaryLinks": []}


def update_task(task_id, *, real_rank, status, screenshot_url=None,
                proxy_ip=None, serp=None):
    body = {
        "realRank": real_rank,
        "auditStatus": [status],                          # MULTI_SELECT -> array
        "lastCheckedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if screenshot_url:
        body["screenshotUrl"] = _links(screenshot_url)
    # ---- recommended added fields; drop any you haven't created ----
    if proxy_ip is not None:
        body["proxyIpUsed"] = proxy_ip
    if serp is not None:
        body["inMapPack"]         = serp["in_map_pack"]
        body["mapPackPosition"]   = serp["map_pack_position"]
        body["aiOverviewPresent"] = serp["ai_overview"]
        body["citedInAio"]        = serp["cited_in_aio"]
        body["rawResult"]         = serp                  # RAW_JSON field
    # ----------------------------------------------------------------
    r = S_T.patch(f"{TWENTY_BASE}/localsearchtasks/{task_id}", json=body)
    if r.status_code >= 300:
        print(f"  ! Twenty write failed {r.status_code}: {r.text[:300]}")
    r.raise_for_status()


# ----------------------------------------------------------------------------
# DuoPlus client  (endpoint names are real; bodies are CONFIRM placeholders)
# ----------------------------------------------------------------------------
class DuoPlus:
    """All calls POST via _duo(). Auth header, method, and {code,data,message}
    envelope are confirmed from DuoPlus's API intro. The per-endpoint PATH and
    BODY field names below are still CONFIRM — fill from each endpoint page."""

    def init_proxy(self, device_id, proxy_id):
        # Proxy Initialization — binds the SOCKS5 proxy; GPS/SIM/timezone then
        # auto-sync from the proxy IP's geolocation.
        return _duo("/cloudphone/proxy/init",                              # CONFIRM path
                    {"ids": [device_id], "proxyId": proxy_id})             # CONFIRM body

    def set_location(self, device_id, lat, lng):
        # Batch Modify Parameters — pin coordinates IF supported. If the API
        # rejects raw coords, drop this and rely on a metro-correct proxy
        # (location follows the IP).
        return _duo("/cloudphone/params/modify",                           # CONFIRM path
                    {"ids": [device_id], "latitude": lat, "longitude": lng})  # CONFIRM body

    def power_on(self, device_id):
        return _duo("/cloudphone/power/on", {"ids": [device_id]})          # CONFIRM path/body

    def status(self, device_id):
        return _duo("/cloudphone/status", {"ids": [device_id]})            # CONFIRM path/body

    def adb(self, device_id, cmd):
        """Execute the ADB command. Returns stdout text from data (CONFIRM key)."""
        data = _duo("/cloudphone/adb/exec",                                # CONFIRM path
                    {"ids": [device_id], "command": cmd})                  # CONFIRM body
        return (data or {}).get("stdout", "")                              # CONFIRM key

    def ui_dump(self, device_id):
        """Dump UIAutomator XML. Returns the hierarchy XML from data (CONFIRM key)."""
        data = _duo("/cloudphone/adb/uiautomator-dump", {"ids": [device_id]})  # CONFIRM
        return (data or {}).get("xml", "")                                 # CONFIRM key

    def wait_booted(self, device_id, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.adb(device_id, "getprop sys.boot_completed").strip() == "1":
                return True
            time.sleep(5)
        raise TimeoutError(f"{device_id} did not boot in {timeout}s")


# ----------------------------------------------------------------------------
# SERP capture + parse
# ----------------------------------------------------------------------------
def open_serp(duo: DuoPlus, device_id, keyword):
    q = quote_plus(keyword)
    url = f"https://www.google.com/search?q={q}&hl={HL}&gl={GL}&num={TOP_N}"
    duo.adb(device_id, f"am force-stop {CHROME_PKG}")
    time.sleep(1)
    duo.adb(device_id,
            f'am start -a android.intent.action.VIEW -d "{url}" {CHROME_PKG}')
    # crude readiness wait; replace with a poll on ui_dump content if you want
    time.sleep(7)
    return url


def get_exit_ip(duo: DuoPlus, device_id):
    """Geo-coherence proof for the proxy-qualification SOP: what IP did the
    phone actually egress from? Uses a text endpoint to keep parsing trivial."""
    out = duo.adb(device_id, "curl -s https://api.ipify.org").strip()
    return out or None


def parse_serp(xml_text, target_domain):
    """
    Heuristic parser over the UIAutomator XML for MOBILE google.

    HONEST WARNING: Chrome's accessibility tree does not cleanly delimit
    'result 1, 2, 3'. This gets you a workable first read and is good enough
    for presence + AIO/map-pack flags + a rough position. For trustworthy
    ORGANIC POSITION, swap this for a chromedriver DOM read (see ALT block at
    bottom) — the DOM has real result containers; the a11y tree does not.

    Returns a dict; real_rank is None if target not found in scanned nodes.
    """
    target = target_domain.lower().lstrip("www.")
    texts = []
    try:
        root = ET.fromstring(xml_text)
        for node in root.iter("node"):
            t = (node.get("text") or "") + " " + (node.get("content-desc") or "")
            t = html.unescape(t).strip()
            if t:
                texts.append(t)
    except ET.ParseError:
        # fall back to raw scan if the dump isn't well-formed
        texts = [html.unescape(x) for x in re.findall(r'(?:text|content-desc)="([^"]+)"', xml_text)]

    blob = " \n ".join(texts).lower()

    ai_overview = any(k in blob for k in ("ai overview", "generative", "search labs"))
    cited_in_aio = ai_overview and target in blob          # coarse; refine per layout
    in_map_pack = any(k in blob for k in ("rating", "reviews", "directions")) and \
                  ("·" in blob or "miles" in blob or "mi ·" in blob)

    # rough organic position: order of first appearance of domain-like tokens
    domains, seen = [], set()
    for t in texts:
        m = re.search(r'\b([a-z0-9-]+\.[a-z]{2,}(?:\.[a-z]{2,})?)\b', t.lower())
        if m:
            d = m.group(1).lstrip("www.")
            if d not in seen and "google." not in d:
                seen.add(d); domains.append(d)
    real_rank = (domains.index(target) + 1) if target in domains else None
    map_pos = None  # fill if you parse the pack block explicitly

    return {
        "real_rank": real_rank,
        "in_map_pack": bool(in_map_pack),
        "map_pack_position": map_pos,
        "ai_overview": bool(ai_overview),
        "cited_in_aio": bool(cited_in_aio),
        "domains_seen": domains[:TOP_N],
    }


def capture_screenshot(duo: DuoPlus, device_id, task_id):
    """Best-effort. screencap -> device file -> upload. Retrieval of the file
    off the device depends on DuoPlus's file/screenshot endpoint (CONFIRM).
    If it fails, we just skip the screenshot and keep the rank."""
    try:
        path = "/sdcard/serp.png"
        duo.adb(device_id, f"screencap -p {path}")
        # CONFIRM: pull the file via DuoPlus file/cloud-drive API, get bytes:
        # png_bytes = duo.pull_file(device_id, path)
        # return upload_to_storage(png_bytes, f"serp/{task_id}.png")
        return None
    except Exception as e:
        print(f"  ~ screenshot skipped: {e}")
        return None


def upload_to_storage(png_bytes, key):
    """Stub: push to Supabase Storage / Cloudflare R2 and return a public URL.
    Wire to whichever bucket your agency stack already uses."""
    raise NotImplementedError("wire to your Supabase/R2 bucket")


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def run_one(duo: DuoPlus, task):
    tid = task["id"]
    kw = task["keyword"]
    domain = (task.get("targetDomain") or "").strip()
    lat, lng = [float(x) for x in task["targetCoords"].split(",")]
    device_id = task.get("deviceId")
    print(f"-> {kw!r} @ {lat},{lng}  domain={domain}  device={device_id}")

    if not device_id:
        update_task(tid, real_rank=None, status="ERROR")
        print("  ! no deviceId on task; skipping")
        return

    try:
        # 1. ensure geo: bind proxy (GPS follows it), optionally pin coords
        if task.get("proxyId"):
            duo.init_proxy(device_id, task["proxyId"])
        duo.set_location(device_id, lat, lng)          # remove if API rejects coords
        # 2. boot
        duo.power_on(device_id)
        duo.wait_booted(device_id)
        # 3. search
        open_serp(duo, device_id, kw)
        ip = get_exit_ip(duo, device_id)
        # 4. scrape
        xml = duo.ui_dump(device_id)
        serp = parse_serp(xml, domain)
        shot = capture_screenshot(duo, device_id, tid)
        # 5. classify + write
        status = "NO_RANK" if serp["real_rank"] is None else "DRIFT"
        if serp["real_rank"] is not None and task.get("apiRank") == serp["real_rank"]:
            status = "MATCH"
        update_task(tid, real_rank=serp["real_rank"], status=status,
                    screenshot_url=shot, proxy_ip=ip, serp=serp)
        print(f"  ok  realRank={serp['real_rank']}  apiRank={task.get('apiRank')}  "
              f"mapPack={serp['in_map_pack']}  aio={serp['ai_overview']}  status={status}")
    except Exception as e:
        print(f"  !! {type(e).__name__}: {e}")
        try:
            update_task(tid, real_rank=None, status="ERROR")
        except Exception:
            pass


def main():
    duo = DuoPlus()
    tasks = get_due_tasks()
    print(f"{len(tasks)} task(s) due")
    for t in tasks:
        run_one(duo, t)
        time.sleep(3)   # gentle pacing between phones/searches


if __name__ == "__main__":
    main()


# ============================================================================
# ALT — chromedriver capture (recommended for trustworthy organic position)
# ----------------------------------------------------------------------------
# Instead of ui_dump + parse_serp, get the real DOM:
#   1. DuoPlus: Batch Enable ADB + Set ADB Connection IP Whitelist (mac mini IP)
#   2. locally:  adb connect <host>:<port>        # host/port from the device detail
#   3. pip install appium-python-client selenium
#   4. Appium UiAutomator2 driver -> chromedriver session against CHROME_PKG,
#      driver.get(url), then:
#         results = driver.find_elements("css selector", "div.g, div[data-rpos]")
#         for i, el in enumerate(results, 1):
#             href = el.find_element("css selector","a").get_attribute("href")
#      Real containers -> real positions, map pack and AIO are distinct nodes
#      you can target by their known wrappers. More setup, far less guessing.
# ============================================================================
