#!/usr/bin/env python3
"""
Bygger docs/data/rutor.json — den rutlista sidan använder för att färga
kartan och slå upp filer utan att fråga Lantmäteriets API i webbläsaren.

    uv run --with requests python bygg-index.py
    (med inloggning: sätt LM_USER och LM_PASS först, om API:et kräver det)

Bakgrund (november 2026)
------------------------
Lantmäteriet har ersatt de gamla 10 km-filerna i dsm-skoglig-copc med
filer på 2,5 × 2,5 km. Id och nedladdningsadress ser ut så här:

    id:   26h019-73725_8900_25
    href: https://dl1.lantmateriet.se/hojd/data/pointcloud/sls/26h019/
          y26h019-73725_8900_25.copc.laz

  26h019      skanningsområde
  73725_8900  rutans sydvästra hörn i enheter om 100 m
              (N 7 372 500, E 890 000 i SWEREF 99 TM)
  25          rutstorlek, 25 = 2,5 km

Sidan visar samma rutnät som filerna, så en ruta på kartan = en position
i katalogen. Rutans namn på kartan är "73725_8900", samma som i id:t.

Utdataformat (docs/data/rutor.json)
-----------------------------------
    {
      "format": 3,
      "uppdaterad": "2026-11-08",
      "antal": 98000,            # antal rutor med data
      "filer": 112000,           # antal filer totalt
      "rutor": {
        "73725_8900": [
          {"o": "26h019", "d": "2026-07-16", "s": 78463831,
           "h": "26h019/y26h019-73725_8900_25.copc.laz"},
          {"o": "20h026", "d": "2020-09-20", "s": 61234567,
           "h": "20h026/y20h026-73725_8900_25.copc.laz"}
        ]
      }
    }

Varje ruta har en lista med filer, nyaste först. Samma ruta kan ha
skannats flera gånger, och då finns en fil per skanning. Fälten har
korta namn för att hålla nere filstorleken:

  o = skanningsområde (Lantmäteriets beteckning, t.ex. 26h019)
  d = datum då området skannades
  s = filens storlek i byte
  h = nedladdningsadressen, kortad: det som står efter
      https://dl1.lantmateriet.se/hojd/data/pointcloud/sls/
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
TILE = 2500                      # rutstorlek i meter, matchar TILE i sidans JS
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
old_format = unknown_id = no_href = bbox_mismatch = 0
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

        area, code = m.group(1).lower(), m.group(4)
        n100, e100 = str(int(m.group(2))), str(int(m.group(3)))  # utan inledande nollor, som sidan
        size_codes[code] += 1
        if code != "25":
            continue  # bara 2,5 km-rutor passar sidans rutnät

        data = (f.get("assets") or {}).get("data") or {}
        href = data.get("href")
        if not href:
            no_href += 1
            continue

        # Kontrollera tolkningen av id:t mot katalogens egen proj:bbox.
        pb = data.get("proj:bbox") or (f.get("properties") or {}).get("proj:bbox")
        if pb and len(pb) == 4 and (round(pb[0]) != int(e100) * 100
                                    or round(pb[1]) != int(n100) * 100):
            bbox_mismatch += 1

        size = data.get("file:size") or 0
        total_bytes += size
        rutor["%s_%s" % (n100, e100)].append({
            "o": area,
            "d": str((f.get("properties") or {}).get("datetime") or "")[:10],
            "s": size,
            "h": short_href(href),
        })

if not rutor:
    sys.exit("Inga rutor hittades — kontrollera nätverk, behörighet och id-format "
             "(%d i gammalt 10 km-format, %d okända)." % (old_format, unknown_id))

for entries in rutor.values():
    entries.sort(key=lambda e: e["d"], reverse=True)   # nyast först

files = sum(len(v) for v in rutor.values())
payload = {
    "format": 3,
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
print("%d rutor, %d filer, %.1f TB totalt" % (len(rutor), files, total_bytes / 1e12))
print("Skrev %s (%.1f MB)" % (OUT, OUT.stat().st_size / 1e6))
print("Storlekskoder i katalogen: %s" % dict(size_codes))
if old_format:
    print("VARNING: %d items i gammalt 10 km-format hoppades över "
          "(filerna togs bort 2 nov 2026)." % old_format)
if unknown_id:
    print("VARNING: %d items med okänt id-format hoppades över." % unknown_id)
if no_href:
    print("VARNING: %d items saknade nedladdningsadress och hoppades över." % no_href)
if bbox_mismatch:
    print("VARNING: %d filer där id:t inte stämmer med proj:bbox — "
          "kontrollera innan du publicerar." % bbox_mismatch)
else:
    print("Id-tolkningen stämmer med proj:bbox för alla filer.")

years = Counter(v[0]["d"][:4] for v in rutor.values())
print("")
print("Senaste skanningsår per ruta (styr färgerna på kartan):")
for y in sorted(years):
    print("  %s: %6d rutor" % (y, years[y]))
same_date = sum(1 for v in rutor.values() if len(v) > 1 and v[0]["d"] == v[1]["d"])
print("%d rutor har flera filer från samma senaste datum (sidan tar med alla)." % same_date)
