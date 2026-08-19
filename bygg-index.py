#!/usr/bin/env python3
"""
Bygger docs/data/rutor.json — den rutlista sidan använder för att slå upp
filnamn utan att fråga Lantmäteriets API i webbläsaren.

Kör om filen någon gång i månaden, eller när nya områden publiceras.

    pip install requests pyproj
    python3 bygg-index.py                    # utan inloggning
    LM_USER=... LM_PASS=... python3 bygg-index.py    # med, om API:et kräver det

Utdataformat (docs/data/rutor.json):
    {
      "uppdaterad": "2026-08-18",
      "antal": 3412,
      "rutor": {
        "627_57": [{"o": "26a019", "d": "2026-04-18", "s": 326973707, "b": [16.98123,56.94210,17.13456,57.03112]}]
      }
    }
  o = skanningsområde (behövs för filnamnet)
  d = insamlingsdatum
  s = filstorlek i byte
  b = bounding box [minLon, minLat, maxLon, maxLat] i WGS84 — hämtad
      direkt från STAC-objektets toppnivå-"bbox", som enligt STAC-spec
      alltid ska finnas i WGS84 (till skillnad från proj:bbox, som
      Lantmäteriets katalog visade sig INTE fylla i). Sidans JS har
      redan en WGS84-konverterare (proj4, EPSG:3006→4326) för att rita
      rutnätet, så samma koordinatsystem kan återanvändas där för att
      jämföra en ruta mot en fils täckning.

Nedladdningslänken byggs av sidan som:
  https://dl1.lantmateriet.se/hojd/data/pointcloud/sls/{o}/m{o}-{ruta}.copc.laz

Utöver rutor.json skrivs docs/data/rutor-blandade-datum.json — en
diagnosrapport över rutor som innehåller filer från MER ÄN ETT datum.
Rapporten simulerar en täckningsalgoritm: går igenom datum nyast till
äldst och tar bara med ett datums filer om det faktiskt minskar
glappet mot rutans nominella kant. Varje ruta klassas som:
  endast_senaste_behovs  — den nyaste filen (eller filerna med samma
                            datum) räcker för att täcka rutan
  flera_datum_behovs     — flera olika datum behöver kombineras för
                            att täcka rutan (t.ex. tre filer från
                            samma flygsäsong, tagna olika dagar)
  gap_kvarstar           — även efter att ha kombinerat ALL
                            tillgänglig data täcks inte hela rutan.
                            Troligen en fysisk gräns (fjällkedjan,
                            riksgränsen) snarare än saknad data.
Rapporten är till för manuell inspektion — den påverkar inte själva
rutor.json och används inte av webbsidan ännu. Täckningskontrollen
görs genom att rutans hörn (SWEREF99 TM) reprojiceras till WGS84 med
pyproj och jämförs mot filernas bbox — ren axel-jämförelse, inte en
exakt polygonberäkning.
"""

import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

try:
    from pyproj import Transformer
except ImportError:
    sys.exit("Saknar pyproj. Kör: pip install pyproj")

API = "https://api.lantmateriet.se/stac-hojd/v1"
COLLECTION = "dsm-skoglig-copc"
PAGE = 2000                      # API:et tillåter upp till 10 000
OUT = Path("docs/data/rutor.json")
DIAG_OUT = Path("docs/data/rutor-blandade-datum.json")
CELL = 10000                     # rutstorlek i meter, matchar CELL i sidans JS
GRID_EPSG = 3006                 # SWEREF99 TM — det koordinatsystem rutnätet bygger på

# Marginal (meter) som en fils bbox får avvika från rutans nominella kant
# och ändå räknas som "täcker rutan". Punktmoln tunnas ofta ut något mot
# flygstråkens ytterkanter, så en helt komplett fil kan ha en bbox som är
# någon meter mindre än den nominella 10x10 km-rutan på varje sida — det
# ska INTE tolkas som en lucka. Justera efter att du sett den faktiska
# glapp-fördelningen i utskriften nedan.
TOLERANCE_M = 50

ID_RE = re.compile(r"^([0-9a-zA-Z]+)-(\d+_\d+)$")

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


def extract_bbox_wgs84(feature):
    """
    Läser ut objektets bounding box i WGS84 (lon/lat) — den enda bbox
    STAC-basspecen garanterar finns, oavsett om katalogen fyller i
    proj-tillägget eller inte (Lantmäteriets katalog gör det inte).

    Returnerar en lista [minLon, minLat, maxLon, maxLat] (avrundat till
    6 decimaler, ~11 cm precision) eller None om fältet oväntat saknas.
    """
    bbox = feature.get("bbox")
    if bbox and len(bbox) == 4:
        return [round(v, 6) for v in bbox]
    return None


_to_wgs84 = Transformer.from_crs(GRID_EPSG, 4326, always_xy=True)


def cell_bounds_wgs84(key):
    """
    Rutans hörn i SWEREF99 TM (utläst ur nyckeln "n_e") reprojicerade
    till WGS84. Alla fyra hörn transformeras var för sig (inte bara två
    diagonala) eftersom rutnätet inte är exakt parallellt med
    longitud/latitud-linjerna.

    Returnerar [minLon, minLat, maxLon, maxLat].
    """
    n_idx, e_idx = key.split("_")
    e0, n0 = int(e_idx) * CELL, int(n_idx) * CELL
    e1, n1 = e0 + CELL, n0 + CELL
    corners = [(e0, n0), (e1, n0), (e1, n1), (e0, n1)]
    lonlat = [_to_wgs84.transform(e, n) for e, n in corners]
    lons = [p[0] for p in lonlat]
    lats = [p[1] for p in lonlat]
    return [min(lons), min(lats), max(lons), max(lats)]


def gaps_in_meters(union_bbox, cell_bbox):
    """
    Räknar ut hur mycket (i meter) union_bbox saknar för att täcka
    cell_bbox på varje sida. Ett positivt tal betyder en lucka av den
    storleken; 0 eller negativt betyder full täckning (eller överlapp)
    på den sidan.

    Grov omräkning grader→meter (lat: ~111 320 m/grad konstant, lon:
    beror på breddgrad) — tillräckligt noggrant för en 10 km ruta,
    inte menat som exakt geodesi.
    """
    u_minlon, u_minlat, u_maxlon, u_maxlat = union_bbox
    c_minlon, c_minlat, c_maxlon, c_maxlat = cell_bbox
    mid_lat = (c_minlat + c_maxlat) / 2
    m_per_deg_lat = 111320
    m_per_deg_lon = 111320 * math.cos(math.radians(mid_lat))

    vast = max(0.0, (u_minlon - c_minlon) * m_per_deg_lon)
    ost = max(0.0, (c_maxlon - u_maxlon) * m_per_deg_lon)
    syd = max(0.0, (u_minlat - c_minlat) * m_per_deg_lat)
    nord = max(0.0, (c_maxlat - u_maxlat) * m_per_deg_lat)
    return {"vast": round(vast, 1), "ost": round(ost, 1),
            "syd": round(syd, 1), "nord": round(nord, 1),
            "max": round(max(vast, ost, syd, nord), 1)}


rutor = {}
skipped = 0
total_bytes = 0
no_bbox = 0

print("Hämtar %s …" % COLLECTION)
for feats in pages():
    for f in feats:
        m = ID_RE.match(f.get("id", ""))
        if not m:
            skipped += 1
            continue
        area, key = m.group(1).lower(), m.group(2)
        data = (f.get("assets") or {}).get("data") or {}
        size = data.get("file:size") or 0
        total_bytes += size

        entry = {
            "o": area,
            "d": str((f.get("properties") or {}).get("datetime") or "")[:10],
            "s": size,
        }

        bbox = extract_bbox_wgs84(f)
        if bbox:
            entry["b"] = bbox
        else:
            no_bbox += 1

        rutor.setdefault(key, []).append(entry)

# nyast först inom varje ruta, så sidan kan ta index 0
for entries in rutor.values():
    entries.sort(key=lambda e: e["d"], reverse=True)

if not rutor:
    sys.exit("Inga rutor hittades — kontrollera nätverk och behörighet.")

payload = {
    "uppdaterad": date.today().isoformat(),
    "antal": len(rutor),
    "rutor": rutor,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
               encoding="utf-8")

files = sum(len(v) for v in rutor.values())
print("")
print("%d rutor, %d filer, %.1f TB totalt i produkten"
      % (len(rutor), files, total_bytes / 1e12))
if skipped:
    print("%d items hoppades över (oväntat id-format)" % skipped)
print("Skrev %s (%.0f kB)" % (OUT, OUT.stat().st_size / 1024))

print("")
if no_bbox:
    print("%d av %d filer saknade en bbox helt (ovanligt — kontrollera API-svaret)." % (no_bbox, files))
    print("Täckningskontrollen nedan kan bara bedöma rutor där minst")
    print("en fil per datum hade en giltig bbox.")
else:
    print("Alla filer hade en giltig bbox (WGS84).")

def merge_bbox(a, b):
    """Slår ihop två bbox (WGS84) till den minsta rektangel som täcker båda."""
    if a is None:
        return list(b)
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def build_coverage(entries, cell_bbox, tolerance):
    """
    Bygger upp täckningen för en ruta genom att gå igenom datum, nyast
    först, och bara ta med ett datums filer om det FAKTISKT minskar
    glappet mot rutans nominella kant. Datum som inte bidrar med något
    nytt (t.ex. en gammal skanning av exakt samma lilla yta som redan
    täcks) hoppas över — de skulle bara vara en onödig extra nedladdning.

    Slutar inte vid första icke-bidragande datumet, utan fortsätter
    igenom hela listan, eftersom ett äldre datum längre bak ändå skulle
    kunna täcka en annan del av rutan.

    Returnerar (använda_datum, kvarvarande_gap, filer_att_ladda_ned).
    """
    dates_sorted = sorted(set(e["d"] for e in entries), reverse=True)
    union = None
    used_dates = []
    used_files = []

    for d in dates_sorted:
        group = [e for e in entries if e["d"] == d]
        group_bboxes = [e["b"] for e in group if "b" in e]
        if not group_bboxes:
            continue

        candidate = union
        for b in group_bboxes:
            candidate = merge_bbox(candidate, b)

        gap_before = gaps_in_meters(union, cell_bbox) if union else {"max": float("inf")}
        gap_after = gaps_in_meters(candidate, cell_bbox)

        if union is None or gap_after["max"] < gap_before["max"] - 0.5:
            union = candidate
            used_dates.append(d)
            used_files.extend(group)

        if gap_after["max"] <= tolerance:
            break  # rutan är redan komplett — inga fler (äldre) datum behövs

    final_gap = gaps_in_meters(union, cell_bbox) if union else None
    return used_dates, final_gap, used_files


# ── diagnos: rutor med filer från mer än ett datum ──────────────────────
blandade = {}
for key, entries in rutor.items():
    dates = sorted(set(e["d"] for e in entries), reverse=True)
    if len(dates) <= 1:
        continue

    cell_bbox = cell_bounds_wgs84(key)
    used_dates, gap, used_files = build_coverage(entries, cell_bbox, TOLERANCE_M)

    if gap is None:
        status = "okant"  # inga bbox alls att bedöma mot
    elif gap["max"] <= TOLERANCE_M:
        status = "endast_senaste_behovs" if len(used_dates) == 1 else "flera_datum_behovs"
    else:
        status = "gap_kvarstar"  # sannolikt en fysisk gräns (fjäll/riksgräns), inte lösbart med fler filer

    blandade[key] = {
        "status": status,
        "anvanda_datum": used_dates,
        "antal_datum_totalt": len(dates),
        "gap_meter": gap,
        "cell_bounds_wgs84": [round(v, 6) for v in cell_bbox],
        "alla_filer": entries,
    }

DIAG_OUT.parent.mkdir(parents=True, exist_ok=True)
# Sortera efter största kvarvarande glapp först — mest misstänkta överst.
blandade_sorted = dict(sorted(
    blandade.items(),
    key=lambda kv: (kv[1]["gap_meter"] or {}).get("max", -1),
    reverse=True,
))
DIAG_OUT.write_text(
    json.dumps(blandade_sorted, indent=2, ensure_ascii=False),
    encoding="utf-8",
)

n_endast_senaste = sum(1 for v in blandade.values() if v["status"] == "endast_senaste_behovs")
n_flera = sum(1 for v in blandade.values() if v["status"] == "flera_datum_behovs")
n_gap = sum(1 for v in blandade.values() if v["status"] == "gap_kvarstar")
n_okant = sum(1 for v in blandade.values() if v["status"] == "okant")

print("")
print("%d rutor har filer från mer än ett datum (marginal: %d m):" % (len(blandade), TOLERANCE_M))
print("  %d klarar sig med bara senaste datumets fil(er)" % n_endast_senaste)
print("  %d behöver filer från FLERA olika datum för att bli kompletta" % n_flera)
print("  %d har ett glapp som INTE går att fylla med tillgänglig data" % n_gap)
if n_okant:
    print("  %d gick inte att bedöma (saknar bbox)" % n_okant)

if n_flera:
    extra_files = sum(len(v["anvanda_datum"]) - 1 for v in blandade.values()
                       if v["status"] == "flera_datum_behovs")
    print("  (%d extra filnedladdningar totalt skulle behövas för dessa %d rutor)"
          % (extra_files, n_flera))

if n_gap:
    # Hur många av "gap_kvarstar"-rutorna beror på att INGET datum hjälpte
    # (dvs. alla tillgängliga filer täcker exakt samma lilla yta) —
    # det är det tydligaste tecknet på en fysisk gräns, inte bristande data.
    n_gap_single = sum(1 for v in blandade.values()
                        if v["status"] == "gap_kvarstar" and len(v["anvanda_datum"]) <= 1)
    print("  Av dessa hade %d ingen förbättring alls från äldre datum"
          % n_gap_single)
    print("  (troligen fjällkedjan, riksgränsen eller annat område utan skanning där)")

print("")
print("Fördelning av kvarvarande glapp (meter) bland rutor med blandade datum:")
buckets = [1, 5, 20, 50, 100, 250, 1000]
counts = [0] * (len(buckets) + 1)
for v in blandade.values():
    g = v["gap_meter"]
    if g is None:
        continue
    m = g["max"]
    placed = False
    for i, edge in enumerate(buckets):
        if m <= edge:
            counts[i] += 1
            placed = True
            break
    if not placed:
        counts[-1] += 1
prev = 0
for edge, n in zip(buckets, counts):
    print("  %4d–%-4d m: %d rutor" % (prev, edge, n))
    prev = edge
print("  >%4d      m: %d rutor" % (prev, counts[-1]))
print("Detaljer (störst glapp överst): %s" % DIAG_OUT)
