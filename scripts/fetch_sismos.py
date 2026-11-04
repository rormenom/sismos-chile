#!/usr/bin/env python3
"""Junta los sismos de Chile desde CSN, USGS, GFZ y EMSC y los agrupa por evento.

Escribe data/sismos.json. Solo usa la biblioteca estándar.
"""
import html
import json
import math
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Caja aproximada de Chile continental + zona de subducción
LAT_MIN, LAT_MAX = -56.5, -17.0
LON_MIN, LON_MAX = -78.0, -66.0
DIAS = 7            # ventana que se muestra
MAG_MIN = 2.5       # magnitud mínima a considerar
MAX_DT = 120        # s: tolerancia en el tiempo de origen entre agencias
MAX_DIST = 30       # km: tolerancia en el epicentro entre agencias

UA = {"User-Agent": "sismos-chile/1.0 (mapa personal)"}
OUT = Path(__file__).resolve().parent.parent / "data" / "sismos.json"


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_time(s):
    s = s.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(s)


def in_box(lat, lon):
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


# ---------- Fuentes ----------

def fdsn_text(base, start, agencia):
    """GFZ y EMSC entregan el formato FDSN 'text' (separado por |)."""
    url = (f"{base}?format=text&minlat={LAT_MIN}&maxlat={LAT_MAX}"
           f"&minlon={LON_MIN}&maxlon={LON_MAX}&minmag={MAG_MIN}"
           f"&start={start:%Y-%m-%dT%H:%M:%S}&orderby=time")
    out = []
    for line in get(url).splitlines():
        if not line or line.startswith("#"):
            continue
        p = line.split("|")
        if len(p) < 13 or not p[10].strip():
            continue
        out.append({
            "agencia": agencia,
            "id": p[0].strip(),
            "t": parse_time(p[1]),
            "lat": float(p[2]), "lon": float(p[3]),
            "prof": float(p[4]) if p[4].strip() else None,
            "mag": float(p[10]),
            "tipo": p[9].strip() or "M",
            "lugar": p[12].strip(),
        })
    return out


def usgs(start):
    url = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
           f"&minlatitude={LAT_MIN}&maxlatitude={LAT_MAX}"
           f"&minlongitude={LON_MIN}&maxlongitude={LON_MAX}"
           f"&minmagnitude={MAG_MIN}&starttime={start:%Y-%m-%dT%H:%M:%S}")
    out = []
    for f in json.loads(get(url))["features"]:
        pr, (lon, lat, depth) = f["properties"], f["geometry"]["coordinates"]
        if pr.get("mag") is None:
            continue
        out.append({
            "agencia": "USGS",
            "id": f["id"],
            "t": datetime.fromtimestamp(pr["time"] / 1000, tz=timezone.utc),
            "lat": lat, "lon": lon, "prof": depth,
            "mag": float(pr["mag"]), "tipo": pr.get("magType") or "M",
            "lugar": pr.get("place") or "",
            "url": pr.get("url"),
        })
    return out


CSN_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CSN_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
CSN_HREF = re.compile(r'href="([^"]+)"', re.I)
TAG = re.compile(r"<[^>]+>")


def csn_dia(fecha):
    """Catálogo diario de sismologia.cl (fechas en UTC)."""
    url = f"https://www.sismologia.cl/sismicidad/catalogo/{fecha:%Y/%m/%Y%m%d}.html"
    try:
        page = get(url)
    except Exception as e:  # el día aún no existe, etc.
        print(f"CSN {fecha:%Y-%m-%d}: {e}", file=sys.stderr)
        return []
    out = []
    for row in CSN_ROW.findall(page):
        tds = CSN_TD.findall(row)
        if len(tds) < 5:
            continue
        first = tds[0]
        href = CSN_HREF.search(first)
        txt0 = [html.unescape(TAG.sub("", x)).strip()
                for x in re.split(r"<br\s*/?>", first, flags=re.I)]
        cells = [html.unescape(TAG.sub(" ", c)).strip() for c in tds]
        try:
            t = parse_time(cells[1])
            coords = re.findall(r"-?\d+\.\d+", cells[2])
            lat, lon = float(coords[0]), float(coords[1])
            prof = re.search(r"\d+(\.\d+)?", cells[3])
            m = re.search(r"(\d+(\.\d+)?)\s*([A-Za-z]+)?", cells[4])
        except (ValueError, IndexError):
            continue
        if not m:
            continue
        mag = float(m.group(1))
        if mag < MAG_MIN:
            continue
        link = None
        if href:
            link = href.group(1)
            if link.startswith("/"):
                link = "https://www.sismologia.cl" + link
        out.append({
            "agencia": "CSN",
            "id": link or f"csn-{t:%Y%m%d%H%M%S}",
            "t": t, "lat": lat, "lon": lon,
            "prof": float(prof.group(0)) if prof else None,
            "mag": mag, "tipo": m.group(3) or "M",
            "lugar": txt0[1] if len(txt0) > 1 else "",
            "url": link,
        })
    return out


def csn(start):
    hoy = datetime.now(timezone.utc).date()
    dias = [(hoy - timedelta(days=i)) for i in range((hoy - start.date()).days + 1)]
    out = []
    for d in dias:
        out += csn_dia(datetime(d.year, d.month, d.day))
    return [e for e in out if e["t"] >= start]


# ---------- Agrupación ----------

def km(a, b):
    r = 6371.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


PRIORIDAD = ["CSN", "USGS", "GFZ", "EMSC"]


def agrupar(reportes):
    # CSN primero: es la referencia para ubicación y lugar
    reportes.sort(key=lambda e: (PRIORIDAD.index(e["agencia"]), -e["mag"]))
    eventos = []
    for r in reportes:
        mejor, mejor_score = None, None
        for ev in eventos:
            if r["agencia"] in ev["reportes"]:
                continue
            ref = ev["ref"]
            dt = abs((r["t"] - ref["t"]).total_seconds())
            d = km(r, ref)
            if dt <= MAX_DT and d <= MAX_DIST:
                score = dt / MAX_DT + d / MAX_DIST
                if mejor_score is None or score < mejor_score:
                    mejor, mejor_score = ev, score
        if mejor:
            mejor["reportes"][r["agencia"]] = r
        else:
            eventos.append({"ref": r, "reportes": {r["agencia"]: r}})

    salida = []
    for ev in eventos:
        ref = ev["ref"]
        mags = [x["mag"] for x in ev["reportes"].values()]
        salida.append({
            "id": f"{ref['t']:%Y%m%d%H%M%S}_{ref['lat']:.2f}_{ref['lon']:.2f}",
            "tiempo": ref["t"].isoformat().replace("+00:00", "Z"),
            "lat": round(ref["lat"], 3),
            "lon": round(ref["lon"], 3),
            "prof": ref["prof"],
            "lugar": ref["lugar"],
            "ref": ref["agencia"],
            "mag_max": max(mags),
            "agencias": {
                a: {"mag": round(x["mag"], 1), "tipo": x["tipo"],
                    "prof": x["prof"], "url": x.get("url")}
                for a, x in ev["reportes"].items()
            },
        })
    salida.sort(key=lambda e: e["tiempo"], reverse=True)
    return salida


def main():
    start = datetime.now(timezone.utc) - timedelta(days=DIAS)
    fuentes = {
        "CSN": lambda: csn(start),
        "USGS": lambda: usgs(start),
        "GFZ": lambda: fdsn_text("https://geofon.gfz.de/fdsnws/event/1/query", start, "GFZ"),
        "EMSC": lambda: fdsn_text("https://www.seismicportal.eu/fdsnws/event/1/query", start, "EMSC"),
    }
    reportes, estado = [], {}
    for nombre, fn in fuentes.items():
        try:
            r = [e for e in fn() if in_box(e["lat"], e["lon"])]
            reportes += r
            estado[nombre] = {"ok": True, "n": len(r)}
        except Exception as e:
            print(f"{nombre}: {e}", file=sys.stderr)
            estado[nombre] = {"ok": False, "error": str(e)[:200]}

    eventos = agrupar(reportes)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "actualizado": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fuentes": estado,
        "eventos": eventos,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(eventos)} eventos · " +
          ", ".join(f"{k}:{v.get('n', 'error')}" for k, v in estado.items()))


if __name__ == "__main__":
    main()
