#!/usr/bin/env python3
"""Tool grafico per geolocalizzare a mano le fermate senza coordinate in
data/gtfs_new/stops.csv (quelle con need_geocoding=1).

Avvia un piccolo server locale con una mappa (Leaflet + OpenStreetMap):
mostra come sfondo tutte le fermate gia' note, e per ogni fermata da
geolocalizzare evidenzia le fermate "vicine" (quelle immediatamente prima
e dopo nella stessa corsa) che hanno gia' una coordinata, cosi' da poter
cliccare nel punto giusto lungo la strada. Ogni click salva subito su
disco: si puo' chiudere e riprendere quando si vuole.

Uso:
  python3 geocode_tool.py [--stops data/gtfs_new/stops.csv]
                           [--trips data/gtfs_new/trips.csv]
                           [--stop-times data/gtfs_new/stop_times.csv]
                           [--routes data/gtfs_new/routes.csv]
                           [--port 8765]

Non richiede dipendenze esterne (solo libreria standard); il browser
carica Leaflet e le tile OpenStreetMap da CDN, quindi serve una
connessione internet lato browser (il server locale invece funziona
offline).
"""
import argparse
import csv
import json
import os
import shutil
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOCK = threading.Lock()
STATE = {}
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geocode_static")
MIME_TYPES = {".css": "text/css", ".js": "application/javascript", ".png": "image/png"}


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv_atomic(path, fieldnames, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})
    os.replace(tmp, path)


def build_state(args):
    stops = read_csv(args.stops)
    stop_fields = list(stops[0].keys()) if stops else []
    stops_by_id = {s["stop_id"]: s for s in stops}

    routes = {r["route_id"]: r["route_short_name"] for r in read_csv(args.routes)}
    trips = read_csv(args.trips)
    route_by_trip = {t["trip_id"]: routes.get(t["route_id"], t["route_id"]) for t in trips}

    stop_times = read_csv(args.stop_times)
    by_trip = {}
    for st in stop_times:
        by_trip.setdefault(st["trip_id"], []).append(st)
    for seq in by_trip.values():
        seq.sort(key=lambda r: int(r["stop_sequence"]))

    routes_by_stop = {}
    neighbors_by_stop = {}
    for trip_id, seq in by_trip.items():
        route_name = route_by_trip.get(trip_id, "?")
        for i, st in enumerate(seq):
            sid = st["stop_id"]
            routes_by_stop.setdefault(sid, set()).add(route_name)
            nb = neighbors_by_stop.setdefault(sid, set())
            if i > 0:
                nb.add(seq[i - 1]["stop_id"])
            if i < len(seq) - 1:
                nb.add(seq[i + 1]["stop_id"])

    return {
        "path": args.stops,
        "fields": stop_fields,
        "stops": stops_by_id,
        "routes_by_stop": {k: sorted(v) for k, v in routes_by_stop.items()},
        "neighbors_by_stop": {k: sorted(v) for k, v in neighbors_by_stop.items()},
    }


def payload():
    with LOCK:
        stops = STATE["stops"]
        items = []
        for sid, s in stops.items():
            lat = s.get("stop_lat") or None
            lon = s.get("stop_lon") or None
            items.append({
                "id": sid,
                "name": s.get("stop_name", ""),
                "lat": float(lat) if lat else None,
                "lon": float(lon) if lon else None,
                "need": s.get("need_geocoding") == "1",
                "routes": STATE["routes_by_stop"].get(sid, []),
                "neighbors": STATE["neighbors_by_stop"].get(sid, []),
            })
        return {"stops": items}


def save_point(stop_id, lat, lon):
    with LOCK:
        stops = STATE["stops"]
        if stop_id not in stops:
            return False, "fermata sconosciuta"
        stops[stop_id]["stop_lat"] = repr(float(lat))
        stops[stop_id]["stop_lon"] = repr(float(lon))
        stops[stop_id]["need_geocoding"] = "0"
        write_csv_atomic(STATE["path"], STATE["fields"], stops.values())
        return True, None


HTML_PAGE = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Geocoding fermate AMA</title>
<link rel="stylesheet" href="/static/leaflet.css" />
<script src="/static/leaflet.js"></script>
<style>
  :root{ --ink:#1d2420; --paper:#f5f2ea; --accent:#2f6b4f; --line:#d8d3c4; }
  *{box-sizing:border-box;}
  body{margin:0;font-family:system-ui,-apple-system,sans-serif;color:var(--ink);background:var(--paper);}
  #app{display:grid;grid-template-columns:340px 1fr;height:100vh;}
  #sidebar{border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden;background:#fff;}
  #map{height:100vh;}
  header{padding:14px 16px;border-bottom:1px solid var(--line);}
  header h1{font-size:1.05rem;margin:0 0 6px;}
  #progress{font-size:0.82rem;color:#555;}
  #progress-bar{height:6px;background:#eee;border-radius:3px;overflow:hidden;margin-top:6px;}
  #progress-fill{height:100%;background:var(--accent);width:0%;transition:width .2s;}
  #search{padding:10px 16px;border-bottom:1px solid var(--line);}
  #search input{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;font-size:0.85rem;}
  #filter{padding:8px 16px;border-bottom:1px solid var(--line);font-size:0.8rem;display:flex;gap:10px;}
  #list{flex:1;overflow-y:auto;}
  .item{padding:9px 16px;border-bottom:1px solid #f0ede3;cursor:pointer;}
  .item:hover{background:#faf8f2;}
  .item.active{background:#eaf2ec;border-left:3px solid var(--accent);padding-left:13px;}
  .item .name{font-weight:600;font-size:0.88rem;}
  .item .meta{font-size:0.74rem;color:#777;margin-top:2px;}
  .badge{display:inline-block;background:#eee;border-radius:4px;padding:1px 5px;font-size:0.68rem;margin-right:3px;}
  #panel{padding:12px 16px;border-top:1px solid var(--line);background:#faf8f2;}
  #panel .name{font-size:1rem;font-weight:700;margin-bottom:4px;}
  #panel .hint{font-size:0.78rem;color:#666;margin-bottom:8px;line-height:1.4;}
  #panel .coords{font-family:ui-monospace,monospace;font-size:0.8rem;margin-bottom:8px;}
  #panel button{padding:7px 12px;border-radius:6px;border:1px solid var(--line);background:#fff;cursor:pointer;font-size:0.82rem;margin-right:6px;}
  #panel button.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
  #panel button:disabled{opacity:.4;cursor:not-allowed;}
  #addr-results{margin-top:6px;font-size:0.78rem;max-height:120px;overflow-y:auto;}
  #addr-results div{padding:4px 6px;border-radius:4px;cursor:pointer;}
  #addr-results div:hover{background:#eee;}
  .leaflet-tooltip.stopname{font-size:11px;}
</style>
</head>
<body>
<div id="app">
  <div id="sidebar">
    <header>
      <h1>Geocoding fermate AMA</h1>
      <div id="progress">caricamento...</div>
      <div id="progress-bar"><div id="progress-fill"></div></div>
    </header>
    <div id="search"><input id="q" placeholder="Cerca fermata..."></div>
    <div id="filter">
      <label><input type="checkbox" id="onlyPending" checked> solo da fare</label>
    </div>
    <div id="list"></div>
    <div id="panel"></div>
  </div>
  <div id="map"></div>
</div>
<script>
const map = L.map('map').setView([42.3498, 13.3995], 14);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

const bgLayer = L.layerGroup().addTo(map);
const neighborLayer = L.layerGroup().addTo(map);
let currentMarker = null;
let pending = null; // {lat, lon}

let STOPS = [];
let byId = {};
let activeId = null;
let history = []; // stop id visitati prima di quello attuale, per "Indietro"

function fmtCoord(v){ return v==null ? '–' : v.toFixed(5); }

async function load(){
  const res = await fetch('/api/data');
  const data = await res.json();
  STOPS = data.stops;
  byId = {};
  STOPS.forEach(s => byId[s.id] = s);
  renderBackground();
  renderList();
  renderProgress();
  if(!activeId){
    const first = STOPS.find(s => s.need);
    if(first) selectStop(first.id);
  }
}

function renderProgress(){
  const total = STOPS.filter(s => s.need || s.lat!=null).length;
  const done = STOPS.filter(s => s.lat!=null).length;
  document.getElementById('progress').textContent = done + ' / ' + total + ' fermate con coordinate';
  document.getElementById('progress-fill').style.width = (total? (100*done/total) : 0) + '%';
}

function renderBackground(){
  bgLayer.clearLayers();
  STOPS.forEach(s => {
    if(s.lat==null) return;
    const m = L.circleMarker([s.lat, s.lon], {
      radius: 3, color: '#888', weight: 1, fillColor:'#aaa', fillOpacity:.6
    });
    m.bindTooltip(s.name, {className:'stopname'});
    m.addTo(bgLayer);
  });
}

function renderList(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const onlyPending = document.getElementById('onlyPending').checked;
  const listEl = document.getElementById('list');
  listEl.innerHTML = '';
  STOPS
    .filter(s => !onlyPending || s.need)
    .filter(s => !q || s.name.toLowerCase().includes(q))
    .sort((a,b) => (b.routes.length - a.routes.length) || a.name.localeCompare(b.name))
    .forEach(s => {
      const div = document.createElement('div');
      div.className = 'item' + (s.id===activeId ? ' active' : '');
      const routesBadges = s.routes.slice(0,6).map(r => '<span class="badge">'+r+'</span>').join('');
      div.innerHTML = '<div class="name">' + (s.lat!=null ? '✓ ' : '') + escapeHtml(s.name) + '</div>' +
        '<div class="meta">' + routesBadges + (s.routes.length>6 ? '+'+(s.routes.length-6) : '') + '</div>';
      div.addEventListener('click', () => goTo(s.id));
      listEl.appendChild(div);
    });
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function goTo(id){
  if(activeId!=null && id!==activeId){ history.push(activeId); }
  selectStop(id);
}

function goBack(){
  if(!history.length) return;
  selectStop(history.pop());
}

function selectStop(id){
  activeId = id;
  pending = null;
  const s = byId[id];
  renderList();
  renderNeighbors(s);
  renderPanel(s);
  if(currentMarker){ map.removeLayer(currentMarker); currentMarker = null; }
  if(s.lat!=null){
    currentMarker = L.marker([s.lat, s.lon], {draggable:true}).addTo(map);
    currentMarker.on('dragend', e => { pending = e.target.getLatLng(); renderPanel(s); });
    map.setView([s.lat, s.lon], 17);
  }
}

function renderNeighbors(s){
  neighborLayer.clearLayers();
  const pts = [];
  (s.neighbors||[]).forEach(nid => {
    const n = byId[nid];
    if(n && n.lat!=null){
      const m = L.circleMarker([n.lat, n.lon], {radius:8, color:'#1d6fb8', weight:2, fillColor:'#4da3ff', fillOpacity:.85});
      m.bindTooltip(n.name + ' (fermata vicina)', {permanent:false});
      m.addTo(neighborLayer);
      pts.push([n.lat, n.lon]);
    }
  });
  if(pts.length){
    map.fitBounds(pts, {maxZoom:16, padding:[60,60]});
  }
}

function renderPanel(s){
  const panel = document.getElementById('panel');
  const coords = pending || (s.lat!=null ? {lat:s.lat, lng:s.lon} : null);
  const nKnown = (s.neighbors||[]).filter(nid => byId[nid] && byId[nid].lat!=null).length;
  panel.innerHTML =
    '<div class="name">' + escapeHtml(s.name) + '</div>' +
    '<div class="hint">Linee: ' + (s.routes.join(', ') || '—') + '<br>' +
    'Fermate vicine con coordinate note: ' + nKnown + (nKnown ? ' (evidenziate in blu)' : '') + '</div>' +
    '<div class="coords">lat: ' + fmtCoord(coords && coords.lat) + ' · lon: ' + fmtCoord(coords && coords.lng) + '</div>' +
    '<button id="btnBack" ' + (history.length?'':'disabled') + '>← Indietro</button>' +
    '<button class="primary" id="btnSave" ' + (coords?'':'disabled') + '>Salva e vai alla prossima</button>' +
    '<button id="btnSkip">Salta</button>' +
    '<div id="addr-results"></div>';
  document.getElementById('btnBack').addEventListener('click', goBack);
  document.getElementById('btnSave').addEventListener('click', () => confirmStop(s.id, coords));
  document.getElementById('btnSkip').addEventListener('click', nextPending);
}

async function confirmStop(id, coords){
  if(!coords) return;
  const res = await fetch('/api/save', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id, lat: coords.lat, lon: coords.lng})
  });
  if(!res.ok){ alert('Errore nel salvataggio'); return; }
  await load();
  nextPending();
}

function nextPending(){
  const next = STOPS.find(s => s.need && s.id!==activeId && byId[s.id].lat==null);
  if(next){ goTo(next.id); }
  else {
    document.getElementById('panel').innerHTML = '<div class="hint">Tutte le fermate visibili sono state geolocalizzate 🎉</div>';
    activeId = null;
    renderList();
  }
}

map.on('click', e => {
  if(!activeId) return;
  pending = e.latlng;
  if(currentMarker){ map.removeLayer(currentMarker); }
  currentMarker = L.marker(e.latlng, {draggable:true}).addTo(map);
  currentMarker.on('dragend', ev => { pending = ev.target.getLatLng(); renderPanel(byId[activeId]); });
  renderPanel(byId[activeId]);
});

document.getElementById('q').addEventListener('input', renderList);
document.getElementById('onlyPending').addEventListener('change', renderList);

load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/data":
            body = json.dumps(payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path.startswith("/static/"):
            self._serve_static(parsed.path[len("/static/"):])
        else:
            self.send_error(404)

    def _serve_static(self, rel_path):
        full = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/save":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            ok, err = save_point(data["id"], data["lat"], data["lon"])
            body = json.dumps({"ok": ok, "error": err}).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stops", default="data/gtfs_new/stops.csv")
    parser.add_argument("--trips", default="data/gtfs_new/trips.csv")
    parser.add_argument("--stop-times", default="data/gtfs_new/stop_times.csv")
    parser.add_argument("--routes", default="data/gtfs_new/routes.csv")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    backup = args.stops + ".bak"
    if not os.path.exists(backup):
        shutil.copy(args.stops, backup)
        print(f"Backup creato: {backup}")

    STATE.update(build_state(args))
    pending = sum(1 for s in STATE["stops"].values() if s.get("need_geocoding") == "1")
    print(f"{len(STATE['stops'])} fermate caricate, {pending} da geolocalizzare.")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Apri {url} (Ctrl+C per fermare). Ogni click salva subito su {args.stops}.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
