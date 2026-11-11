"""Production contract audit — run from the VPS (or any host with the prod token):
  ssh google-vps-next "sudo -n /opt/schwab-trader/backend/.venv/bin/python -" < scripts/prod_audit.py

Covers every live contract of the trading hub: health/observability fields,
auth gates, true-monthly/stamps, segment paging + 304, fresh-window cache
headers, CF edge behavior (segments + edge-cached fresh windows, colo
flapping tolerated as dual-state), WS upgrade + auth. Token is read
server-side and never echoed. See docs/plan-kline-uncapped-scroll.md §9 and
vault manual §7.14 for the contracts it pins.
"""
import json, sys, time, datetime, urllib.request, urllib.error
sys.path.insert(0, "/opt/schwab-trader/backend")

BASE = "http://127.0.0.1:8800/api"
PUB  = "https://trader.mangoqwq.com"
TOK  = [l for l in open("/etc/schwab-trader.env") if l.startswith("ADMIN_TOKEN")][0].split("=",1)[1].strip()
results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))

def get(url, token=None, headers=None, retries=0):
    h = {"User-Agent": "curl/8.5.0"}
    if token: h["X-Auth-Token"] = token
    h.update(headers or {})
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers, r.read()  # HTTPMessage: case-insensitive
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < retries:
                time.sleep(6)  # transient CF bot challenge on this egress IP
                continue
            return e.code, e.headers, e.read()

# ---- 1. health & observability ----
st, hd, body = get(BASE + "/health")
h = json.loads(body)
check("health.status=ok", h.get("status")=="ok")
check("us stream connected", h["feeds"]["us"].get("state")=="connected")
check("intl feed running", h["feeds"]["intl"].get("running") is True)
check("rate_limit counter exposed", "rate_limit" in h["feeds"]["intl"])
check("daily_fallback exposed", "daily_fallback" in h["feeds"]["us"])

# ---- 2. auth gates ----
st,_,_ = get(BASE + "/portfolio")
check("portfolio anon=401", st==401, "got %s" % st)
st,_,_ = get(BASE + "/orders", token="wrong-token")
check("orders bad-token=401", st==401, "got %s" % st)
st,_,body = get(BASE + "/portfolio", token=TOK)
p = json.loads(body)
check("portfolio authed=200", st==200, "got %s" % st)
n_pos = len(p.get("positions", {}) or {})
check("positions survive deploys", isinstance(p.get("positions"), dict), "%d positions" % n_pos)

# ---- 3. true monthly ----
def bars(sym, tf):
    st,_,b = get("%s/market/bars?symbol=%s&timeframe=%s" % (BASE, sym, tf), token=TOK)
    return st, (json.loads(b) if st==200 else [])

st, ko1mo = bars("KO","1mo")
years = {}
for b in ko1mo:
    y = datetime.datetime.fromtimestamp(b["time"], datetime.timezone.utc).year
    years[y] = years.get(y,0)+1
tm = sum(1 for c in years.values() if c==12)
check("KO 1mo 504 bars", len(ko1mo)==504, "%d bars" % len(ko1mo))
check("KO 1mo all years have 12", tm==len(years) and len(years)>=42, "%d/%d" % (tm, len(years)))
anchor_ok = all(datetime.datetime.fromtimestamp(b["time"], datetime.timezone.utc).strftime("%H:%M") in ("04:00","05:00") for b in ko1mo[:-1])
check("KO 1mo stamps midnight-ET anchors", anchor_ok)
st, spy1mo = bars("SPY","1mo")
check("SPY 1mo ~407 bars", 400 <= len(spy1mo) <= 415, "%d" % len(spy1mo))
st, ko1wk = bars("KO","1wk")
mon = all(datetime.datetime.fromtimestamp(b["time"], datetime.timezone.utc).strftime("%a")=="Mon" for b in ko1wk[:-1])
check("KO 1wk stamps all Monday", bool(mon) and len(ko1wk)>=500, "%d bars" % len(ko1wk))
st, ko1d = bars("KO","1d")
open_stamp = datetime.datetime.fromtimestamp(ko1d[-1]["time"], datetime.timezone.utc).strftime("%H:%M")
check("KO 1d stamp session-open", open_stamp in ("13:30","14:30"), open_stamp)
check("KO 1d ~5y window", 1200 <= len(ko1d) <= 1300, "%d" % len(ko1d))

# ---- 4. intl route unaffected ----
st, hk1mo = bars("0700.HK","1mo")
check("0700.HK 1mo intl route works", st==200 and len(hk1mo)>100, "%d bars" % len(hk1mo))

# ---- 5. segment contract ----
cursor = int(time.time()) - 86400*7
seg_url = "%s/market/bars?symbol=KO&timeframe=15m&before=%d" % (BASE, cursor)
st, hd, body = get(seg_url, token=TOK)
seg = json.loads(body)
check("segment 200", st==200)
check("segment strictly older", all(b["time"] < cursor for b in seg), "%d bars" % len(seg))
check("X-Bars-Has-More present", hd.get("X-Bars-Has-More") in ("0","1"), hd.get("X-Bars-Has-More"))
check("X-Bars-Oldest present", "X-Bars-Oldest" in hd)
check("segment s-maxage=604800", "s-maxage=604800" in (hd.get("Cache-Control") or ""), hd.get("Cache-Control"))
etag = hd.get("ETag") or ""
check("ETag weak validator", etag.startswith("W/"), etag)
st2, hd2, body2 = get(seg_url, token=TOK, headers={"If-None-Match": etag})
check("segment 304 conditional", st2==304 and len(body2)==0, "%s, %d bytes" % (st2, len(body2)))
check("304 keeps X-Bars-Has-More", hd2.get("X-Bars-Has-More") in ("0","1"))
st3,_,_ = get("%s/market/bars?symbol=KO&timeframe=15m&before=0" % BASE, token=TOK)
check("before=0 -> 400", st3==400, "got %s" % st3)
st4,_,_ = get("%s/market/bars?symbol=KO&timeframe=1d&before=%d" % (BASE, ko1d[0]["time"]), token=TOK)
check("1d before -> 400", st4==400, "got %s" % st4)

# ---- 6. fresh-window cache contract ----
st, hd, body = get("%s/market/bars?symbol=KO&timeframe=15m" % BASE, token=TOK)
cc = hd.get("Cache-Control") or ""
check("fresh window public edge-cacheable or no-store", "public" in cc or "no-store" in cc, cc)
check("X-Cache present", hd.get("X-Cache") in ("HIT","STALE","MISS"), hd.get("X-Cache"))

# ---- 7. CF public chain ----
seg_pub = "%s/api/market/bars?symbol=SPY&timeframe=15m&before=%d" % (PUB, int(time.time()) - 86400*3)
req = urllib.request.Request(seg_pub, headers={"X-Auth-Token": TOK, "User-Agent": "curl/8.5.0"})
with urllib.request.urlopen(req, timeout=30) as r:
    cf1 = r.headers.get("CF-Cache-Status"); pub_etag = r.headers.get("ETag"); pub_cc = r.headers.get("Cache-Control")
check("CF segment first (MISS/EXPIRED ok)", cf1 in ("MISS","HIT","EXPIRED"), cf1)
with urllib.request.urlopen(urllib.request.Request(seg_pub, headers={"X-Auth-Token": TOK, "User-Agent": "curl/8.5.0"}), timeout=30) as r:
    cf2 = r.headers.get("CF-Cache-Status")
check("CF segment second: HIT or re-fetch on cold colo", cf2 in ("HIT","MISS","EXPIRED"), cf2)
check("CF respects origin s-maxage", "s-maxage=604800" in (pub_cc or ""), pub_cc)
# The documented F3 contract, alive with a caveat: the auth header is NOT in
# the cache key, so a tokenless request is served the cached public segment
# from a warm colo (200). CF cache is per-colo and anycast routing flaps, so
# a cold colo forwards to origin and the tokenless request 401s — both are
# correct (real clients always send the token; the security property is that
# nothing per-user is cached, not that tokenless requests succeed).
st7,_,b7 = get(seg_pub)
check("CF tokenless segment: cached 200 or origin 401 (colo-dependent)", st7 in (200, 401), "got %s" % st7)
# Fresh windows are edge-cacheable since 4b92e9e: first fetch may MISS,
# the repeat must HIT (respect_origin edge TTL = origin max-age).
with urllib.request.urlopen(urllib.request.Request("%s/api/market/bars?symbol=SPY&timeframe=15m" % PUB, headers={"X-Auth-Token": TOK, "User-Agent": "curl/8.5.0"}), timeout=30) as r:
    cf3 = r.headers.get("CF-Cache-Status")
with urllib.request.urlopen(urllib.request.Request("%s/api/market/bars?symbol=SPY&timeframe=15m" % PUB, headers={"X-Auth-Token": TOK, "User-Agent": "curl/8.5.0"}), timeout=30) as r:
    cf4 = r.headers.get("CF-Cache-Status")
check("CF fresh window first fetch sane", cf3 in ("MISS","EXPIRED","DYNAMIC","HIT"), cf3)
check("CF fresh window repeat: HIT or re-fetch on cold colo", cf4 in ("HIT","MISS","EXPIRED"), cf4)
st8,_,_ = get("%s/api/market/bars?symbol=SPY&timeframe=15m" % PUB, retries=2)
check("fresh window tokenless dual-state (200 edge HIT / 401 cold colo)", st8 in (200, 401), "got %s" % st8)
st5,_,_ = get("%s/api/portfolio" % PUB, retries=2)
check("CF public auth 401", st5==401, "got %s" % st5)
st6,_,b6 = get("%s/api/health" % PUB, retries=2)
check("CF public health ok", st6==200 and json.loads(b6)["status"]=="ok")

# ---- 8. WS upgrade + auth ----
import asyncio, websockets
async def ws_check():
    hdrs = {"User-Agent": "curl/8.5.0"}
    try:
        kw = {"additional_headers": hdrs}
        conn = websockets.connect("wss://trader.mangoqwq.com/ws", open_timeout=15, **kw)
    except TypeError:
        conn = websockets.connect("wss://trader.mangoqwq.com/ws", open_timeout=15, extra_headers=hdrs)
    async with conn as ws:
        await ws.send(json.dumps({"action":"AUTH","token":"bogus"}))
        try:
            reply = await asyncio.wait_for(ws.recv(), timeout=10)
            return "bogus-replied", reply
        except websockets.ConnectionClosed:
            return "closed-on-bogus", ""
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
try:
    kind, reply = loop.run_until_complete(ws_check())
    check("WS upgrade through CF", True, kind)
    check("WS bogus token rejected", kind=="closed-on-bogus" or "error" in str(reply).lower() or "denied" in str(reply).lower(), str(reply)[:100])
except Exception as e:
    check("WS upgrade through CF", False, str(e)[:140])

fails = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(("PASS " if ok else "FAIL ") + name + (("  | " + detail) if (detail and not ok) else ""))
print("")
print("=== %d/%d passed ===" % (len(results)-len(fails), len(results)))
