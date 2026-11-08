#!/usr/bin/env python3
"""
Bygger docs/data/rutor.json — den rutlista sidan använder för att slå upp
filer utan att fråga Lantmäteriets API i webbläsaren.

    pip install requests
    python3 bygg-index.py                          # utan inloggning
    LM_USER=... LM_PASS=... python3 bygg-index.py  # med, om API:et kräver det

Bakgrund (november 2026)
------------------------
Lantmäteriet har ersatt de gamla 10 km-filerna i dsm-skoglig-copc med
2,5 km-filer. Id och filnamn ser nu ut så här:

    id:   26h019-73725_8900_25
    href: https://dl1.lantmateriet.se/hojd/data/pointcloud/sls/26h019/
          y26h019-73725_8900_25.copc.laz

  26h019      skanningsområde
  73725_8900  rutans nedre vänstra hörn i enheter om 100 m
              (N 7 372 500, E 890 000 i SWEREF 99 TM)
  25          rutstorlek, 25 = 2,5 km

Sidan byggde tidigare nedladdningslänken själv efter ett fast mönster.
Det slutade fungera när Lantmäteriet bytte filnamn. Nu sparas katalogens
egen href för varje fil, så sidan aldrig behöver gissa en adress.

Utdataformat (docs/data/rutor.json)
-----------------------------------
    {
      "format": 2,
      "uppdaterad": "2026-11-08",
      "antal": 4418,          # antal 10 km-rutor
      "filer": 98765,         # antal filer totalt
      "rutor": {
        "737_89": [
          {"t": "10", "o": "26h019", "d": "2026-07-16", "s": 78463831,
           "h": "26h019/y26h019-73725_8900_25.copc.laz"}
        ]
      }
    }

  Nyckel = 10 km-rutan ("norr_öst" i enheter om 10 km), samma rutnät som
           sidan ritar. Varje 10 km-ruta rymmer upp till 4 × 4 delrutor.
  t = delrutans plats i 10 km-rutan, "rad" + "kolumn" räknat från
      sydväst (0–3). "10" = andra raden norrut, första kolumnen.
  o = skanningsområde
  d = insamlingsdatum
  s = filstorlek i byte
  h = href relativt https://dl1.lantmateriet.se/hojd/data/pointcloud/sls/
      Ligger filen någon annanstans sparas hela adressen.
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import requests

API = "https://api.lantmateriet.se/stac-hojd/v1"
COLLECTION = "dsm-skoglig-copc"
PAGE = 2000                      # API:et tillåter upp till 10 000
OUT = Path("docs/data/rutor.json")
CELL = 10000                     # 10 km-rutan, matchar CELL i sidans JS
DL_BASE = "https://dl1.lantmateriet.se/hojd/data/pointcloud/sls/"

# 26h019-73725_8900_25 → område, norr/100 m, öst/100 m, storlekskod
ID_RE = re.compile(r"^([0-9a-z]+)-(\d+)_(\d+)_(\d+)$", re.IGNORECASE)
# Det gamla 10 km-formatet, 26a019-627_57. Filerna är borttagna.
OLD_ID_RE = re.compile(r"^[0-9a-z]+-\d+_\d+$", re.IGNORECASE)

session = requests.Session()
user, password = os.environ.get("LM_USER"), os.environ.get("LM_PASS")
if user and password:
    session.auth = (user, password)
    print("Använder inloggning för %s" % user)
else:
    print("Kör utan inloggning — metadata är öppen i dagsläget.")


def pages():
    """Bläddrar igenom hela collectionen och ger en sida i taget."""
    url = "%s/collections/%s/items?limit=%d" % (API, COLLECTION, PAGE)
    n = 0
    while url:
        n += 1
        r = session.get(url, timeout=180)
        if r.status_code == 401:
            sys.exit("401: fel uppgifter. Använd ett systemkonto från Geotorget.")
        if r.status_code == 403:
            sys.exit("403: kontot saknar behörighet till Laserdata Skog.")
        r.raise_for_status()
        page = r.json()
        feats = page.get("features", [])
        print("  sida %-3d %5d items" % (n, len(feats)))
        yield feats
        url = next((l["href"] for l in page.get("links", [])
                    if l.get("rel") == "next"), None)


def short_href(href):
    """Kortar adressen om den ligger under DL_BASE, annars hela adressen."""
    return href[len(DL_BASE):] if href.startswith(DL_BASE) else href


rutor = defaultdict(list)
old_format = 0
unknown_id = 0
no_href = 0
bbox_mismatch = 0
size_codes = Counter()
total_bytes = 0

print("Hämtar %s …" % COLLECTION)
for feats in pages():
    for f in feats:
        fid = f.get("id", "")
        m = ID_RE.match(fid)
        if not m:
            if OLD_ID_RE.match(fid):
                old_format += 1
            else:
                unknown_id += 1
            continue

        area, n100, e100, code = m.group(1).lower(), int(m.group(2)), int(m.group(3)), m.group(4)
        size_codes[code] += 1
        if code != "25":
            # Bara 2,5 km-rutor väntas. Andra storlekar räknas men tas inte med,
            # eftersom sidans indelning i 4 × 4 delrutor då inte stämmer.
            continue

        data = (f.get("assets") or {}).get("data") or {}
        href = data.get("href")
        if not href:
            no_href += 1
            continue

        n0, e0 = n100 * 100, e100 * 100
        # Kontrollera tolkningen av id:t mot katalogens egen proj:bbox.
        pb = data.get("proj:bbox") or (f.get("properties") or {}).get("proj:bbox")
        if pb and len(pb) == 4 and (round(pb[0]) != e0 or round(pb[1]) != n0):
            bbox_mismatch += 1

        key = "%d_%d" % (n0 // CELL, e0 // CELL)
        sub = "%d%d" % ((n0 % CELL) // 2500, (e0 % CELL) // 2500)
        size = data.get("file:size") or 0
        total_bytes += size

        rutor[key].append({
            "t": sub,
            "o": area,
            "d": str((f.get("properties") or {}).get("datetime") or "")[:10],
            "s": size,
            "h": short_href(href),
        })

if not rutor:
    sys.exit("Inga rutor hittades — kontrollera nätverk, behörighet och id-format "
             "(%d i gammalt 10 km-format, %d okända)." % (old_format, unknown_id))

# Sortera: delruta, sedan nyast först, så sidan kan ta första träffen per delruta.
for entries in rutor.values():
    entries.sort(key=lambda e: e["d"], reverse=True)
    entries.sort(key=lambda e: e["t"])

files = sum(len(v) for v in rutor.values())
payload = {
    "format": 2,
    "uppdaterad": date.today().isoformat(),
    "antal": len(rutor),
    "filer": files,
    "rutor": dict(sorted(rutor.items())),
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
               encoding="utf-8")

# ── sammanfattning ─────────────────────────────────────────────────────
print("")
print("%d rutor (10 km), %d filer, %.1f TB totalt"
      % (len(rutor), files, total_bytes / 1e12))
print("Skrev %s (%.1f MB)" % (OUT, OUT.stat().st_size / 1e6))

print("")
print("Storlekskoder i katalogen: %s" % dict(size_codes))
if old_format:
    print("VARNING: %d items i gammalt 10 km-format hoppades över." % old_format)
    print("  Filerna togs bort 2 nov 2026. Kontrollera ett av dem med curl om antalet är stort.")
if unknown_id:
    print("VARNING: %d items med okänt id-format hoppades över." % unknown_id)
if no_href:
    print("VARNING: %d items saknade assets.data.href och hoppades över." % no_href)
if bbox_mismatch:
    print("VARNING: %d items där id:t inte stämmer med proj:bbox." % bbox_mismatch)
    print("  Tolkningen av id-formatet kan vara fel — kontrollera innan du publicerar.")
else:
    print("Id-tolkningen stämmer med proj:bbox för alla filer.")

# Hur ser rutorna ut? Hjälper att avgöra om sidans logik (nyaste filen per
# delruta) räcker.
full = sum(1 for v in rutor.values() if len({e["t"] for e in v}) == 16)
multi_date = 0
same_date_multi = 0
for v in rutor.values():
    per_sub = defaultdict(list)
    for e in v:
        per_sub[e["t"]].append(e)
    for es in per_sub.values():
        if len({e["d"] for e in es}) > 1:
            multi_date += 1
        newest = es[0]["d"]
        if sum(1 for e in es if e["d"] == newest) > 1:
            same_date_multi += 1

print("")
print("%d av %d rutor har alla 16 delrutor (resten ligger vid kust, gräns eller fjäll)."
      % (full, len(rutor)))
print("%d delrutor finns från flera datum (sidan väljer det nyaste)." % multi_date)
print("%d delrutor har flera filer med samma nyaste datum (sidan tar med alla)."
      % same_date_multi)
