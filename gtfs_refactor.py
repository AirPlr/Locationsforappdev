#!/usr/bin/env python3
"""Rigenera un feed GTFS (routes/stops/trips/stop_times) usando gli orari
freschi estratti da ama_orari_crawler.py, a partire da un vecchio export
GTFS che non conteneva tutte le linee (es. mancavano le linee A e B).

Cosa fa:
  - routes.csv: una riga per ogni linea trovata nel crawler (46 -> quelle
    con almeno una corsa estratta). Se il route_short_name coincide (a
    meno di maiuscole/spazi) con una linea del vecchio routes.csv, viene
    riusato lo stesso route_id; altrimenti ne viene creato uno nuovo.
  - stops.csv: una riga per ogni fermata distinta trovata nei PDF. Le
    coordinate vengono ereditate dal vecchio stops.csv quando il nome
    normalizzato corrisponde esattamente; le fermate senza corrispondenza
    (tipicamente quelle delle linee A/B, mai censite prima) restano senza
    lat/lon e vanno geolocalizzate a mano.
  - trips.csv / stop_times.csv: rigenerati da zero per ogni corsa estratta
    dai PDF, con stop_sequence che salta le fermate non servite ("-").

Limiti noti (i PDF degli orari non contengono questi dati):
  - Nessuna geometria di percorso: shape_id viene lasciato vuoto per tutte
    le nuove corse (il vecchio shapes.csv viene copiato invariato, ma non
    è collegato alle nuove trips).
  - service_id e' dedotto dalla categoria della linea (Feriale=1,
    Festivo=2, Scolastico=3), non da un calendario reale: verificare/
    correggere in un secondo momento con un vero calendar.csv.
  - Le fermate delle tabelle dove il nome non e' stato ricostruibile
    (vedi README del crawler) vengono etichettate come "Fermata N (da
    verificare)" e marcate need_geocoding=1 in stops.csv.

Uso:
  python3 gtfs_refactor.py \
      --old-gtfs-dir /path/alla/cartella/con/routes.csv,stops.csv,... \
      --schedules data/ama_orari/schedules.json \
      --out-dir data/gtfs_new
"""
import argparse
import csv
import json
import os
import re
import unicodedata

SERVICE_ID_BY_CATEGORY = {
    "Feriale": "1",
    "Festivo": "2",
    "Scuole": "3",
    "Stazione": "2",
}

ROUTES_FIELDS = [
    "route_id", "agency_id", "route_short_name", "route_long_name",
    "route_desc", "route_type", "route_url", "route_color", "route_text_color",
]
STOPS_FIELDS = [
    "stop_id", "stop_code", "stop_name", "stop_desc", "stop_lat", "stop_lon",
    "zone_id", "stop_url", "location_type", "parent_station", "stop_timezone",
    "wheelchair_boarding", "need_geocoding",
]
TRIPS_FIELDS = [
    "route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name",
    "direction_id", "block_id", "shape_id", "wheelchair_accessible",
]
STOP_TIMES_FIELDS = [
    "trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence",
    "stop_headsign", "pickup_type", "drop_off_type", "shape_dist_traveled",
    "timepoint",
]


def normalize_name(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def build_route_id_map(old_routes):
    """normalized route_short_name -> route_id, e id massimo esistente."""
    by_name = {}
    max_id = 0
    for r in old_routes:
        by_name[normalize_name(r["route_short_name"])] = r["route_id"]
        max_id = max(max_id, int(r["route_id"]))
    return by_name, max_id


def build_stop_id_map(old_stops):
    """normalized stop_name -> (stop_id, lat, lon), e id massimo esistente."""
    by_name = {}
    max_id = 0
    for s in old_stops:
        key = normalize_name(s["stop_name"])
        if key and key not in by_name:
            by_name[key] = (s["stop_id"], s["stop_lat"], s["stop_lon"])
        max_id = max(max_id, int(s["stop_id"]))
    return by_name, max_id


def route_long_name(line):
    if line.get("itinerario"):
        return f"LINEA {line['linea']} {line['itinerario']}".upper()
    return f"LINEA {line['linea']}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old-gtfs-dir", required=True, help="cartella col vecchio routes.csv, stops.csv, trips.csv, stop_times.csv, shapes.csv")
    parser.add_argument("--schedules", default="data/ama_orari/schedules.json")
    parser.add_argument("--out-dir", default="data/gtfs_new")
    args = parser.parse_args()

    old_routes = read_csv(os.path.join(args.old_gtfs_dir, "routes.csv"))
    old_stops = read_csv(os.path.join(args.old_gtfs_dir, "stops.csv"))
    lines = json.load(open(args.schedules, encoding="utf-8"))
    lines = [l for l in lines if sum(len(t["corse"]) for t in l.get("tabelle", [])) > 0]

    route_id_by_name, next_route_id = build_route_id_map(old_routes)
    stop_id_by_name, next_stop_id = build_stop_id_map(old_stops)

    new_routes = []
    new_stops = {}  # stop_id -> row, per evitare duplicati
    new_trips = []
    new_stop_times = []

    next_trip_id = 1
    stats = {"linee": 0, "corse": 0, "fermate_nuove": 0, "fermate_riusate": 0, "fermate_da_verificare": 0}

    for line in lines:
        stats["linee"] += 1
        key = normalize_name(line["linea"])
        if key in route_id_by_name:
            route_id = route_id_by_name[key]
        else:
            next_route_id += 1
            route_id = str(next_route_id)
            route_id_by_name[key] = route_id

        new_routes.append({
            "route_id": route_id,
            "agency_id": "1",
            "route_short_name": line["linea"],
            "route_long_name": route_long_name(line),
            "route_desc": "",
            "route_type": "3",
            "route_url": line.get("pdf_url", ""),
            "route_color": "",
            "route_text_color": "",
        })

        service_id = SERVICE_ID_BY_CATEGORY.get(line.get("categoria"), "1")

        for direction_id, tabella in enumerate(line.get("tabelle", [])):
            fermate = tabella.get("fermate")
            for corsa in tabella["corse"]:
                orari = corsa["orari"]
                served = [(i, t) for i, t in enumerate(orari) if t]
                if not served:
                    continue

                stats["corse"] += 1
                trip_id = str(next_trip_id)
                next_trip_id += 1

                stop_ids_in_order = []
                for pos, (i, _) in enumerate(served):
                    if fermate and i < len(fermate) and fermate[i]:
                        stop_name = fermate[i]
                        norm = normalize_name(stop_name)
                        if norm in stop_id_by_name:
                            stop_id, lat, lon = stop_id_by_name[norm]
                            need_geocoding = "0" if lat else "1"
                            stats["fermate_riusate"] += 1
                        else:
                            next_stop_id += 1
                            stop_id = str(next_stop_id)
                            lat, lon = "", ""
                            need_geocoding = "1"
                            stop_id_by_name[norm] = (stop_id, "", "")
                            stats["fermate_nuove"] += 1
                    else:
                        stop_name = f"{line['linea']} - fermata {i + 1} (da verificare)"
                        norm = f"__unverified__{route_id}__{i}"
                        if norm in stop_id_by_name:
                            stop_id, lat, lon = stop_id_by_name[norm]
                        else:
                            next_stop_id += 1
                            stop_id = str(next_stop_id)
                            lat, lon = "", ""
                            stop_id_by_name[norm] = (stop_id, "", "")
                        need_geocoding = "1"
                        stats["fermate_da_verificare"] += 1

                    if stop_id not in new_stops:
                        new_stops[stop_id] = {
                            "stop_id": stop_id,
                            "stop_code": "",
                            "stop_name": stop_name,
                            "stop_desc": "",
                            "stop_lat": lat,
                            "stop_lon": lon,
                            "zone_id": "",
                            "stop_url": "",
                            "location_type": "0",
                            "parent_station": "",
                            "stop_timezone": "",
                            "wheelchair_boarding": "",
                            "need_geocoding": need_geocoding,
                        }
                    stop_ids_in_order.append(stop_id)

                headsign = (fermate[served[-1][0]] if fermate and served[-1][0] < len(fermate) and fermate[served[-1][0]] else None)
                headsign = headsign or corsa.get("variante_raw") or line["linea"]

                new_trips.append({
                    "route_id": route_id,
                    "service_id": service_id,
                    "trip_id": trip_id,
                    "trip_headsign": headsign,
                    "trip_short_name": corsa.get("variante_raw") or "",
                    "direction_id": "0" if direction_id == 0 else "1",
                    "block_id": "",
                    "shape_id": "",
                    "wheelchair_accessible": "",
                })

                for seq, (stop_id, (i, t)) in enumerate(zip(stop_ids_in_order, served), start=1):
                    new_stop_times.append({
                        "trip_id": trip_id,
                        "arrival_time": f"{t}:00",
                        "departure_time": f"{t}:00",
                        "stop_id": stop_id,
                        "stop_sequence": seq,
                        "stop_headsign": "",
                        "pickup_type": "0",
                        "drop_off_type": "0",
                        "shape_dist_traveled": "",
                        "timepoint": "1",
                    })

    write_csv(os.path.join(args.out_dir, "routes.csv"), ROUTES_FIELDS, new_routes)
    write_csv(os.path.join(args.out_dir, "stops.csv"), STOPS_FIELDS, new_stops.values())
    write_csv(os.path.join(args.out_dir, "trips.csv"), TRIPS_FIELDS, new_trips)
    write_csv(os.path.join(args.out_dir, "stop_times.csv"), STOP_TIMES_FIELDS, new_stop_times)

    print(f"Linee incluse: {stats['linee']}")
    print(f"Corse (trips) generate: {stats['corse']}")
    print(f"Fermate totali distinte: {len(new_stops)}")
    print(f"  - riusate dal vecchio stops.csv (coordinate note): {stats['fermate_riusate']}")
    print(f"  - nuove, senza coordinate: {stats['fermate_nuove']}")
    print(f"  - non identificate ('da verificare', senza nome ne' coordinate): {stats['fermate_da_verificare']}")
    con_coord = sum(1 for s in new_stops.values() if s["stop_lat"])
    print(f"Fermate con coordinate: {con_coord}/{len(new_stops)}")
    print(f"\nScritti in {args.out_dir}: routes.csv, stops.csv, trips.csv, stop_times.csv")
    print("shapes.csv NON rigenerato: nessuna geometria di percorso disponibile dai PDF orari.")


if __name__ == "__main__":
    main()
