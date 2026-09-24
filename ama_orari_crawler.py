#!/usr/bin/env python3
"""Crawler per gli orari dei PDF delle linee AMA L'Aquila.

Pipeline:
  1. Scarica la pagina "elenco linee" e ne estrae, per ogni linea, il PDF
     dell'orario, la categoria (Feriale/Festivo/Scuole/...) e l'itinerario.
  2. Scarica tutti i PDF trovati.
  3. Estrae dalle tabelle dei PDF le singole corse (orari di passaggio).
  4. Esporta i dati in data/ama_orari/ come JSON e CSV.

Uso:
  python3 ama_orari_crawler.py [--skip-download] [--skip-crawl]

Richiede: pdfplumber (pip install pdfplumber)
"""
import argparse
import csv
import json
import os
import re
import time
import urllib.request

import pdfplumber

BASE_URL = "https://www.ama.laquila.it/linee-e-orari/elenco-linee-ama/"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ama_orari")
PDF_DIR = os.path.join(DATA_DIR, "pdf")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
TIME_RE = re.compile(r"^\d{1,2}[:.,]\d{2}$")


def fetch_url(url, attempts=6):
    """Scarica un URL con retry: la connessione al sito AMA è instabile."""
    last_err = None
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            time.sleep(2 * i)
    raise RuntimeError(f"Impossibile scaricare {url}: {last_err}")


def crawl_lines_index():
    """Estrae dall'HTML della pagina elenco-linee la lista linea->PDF orario."""
    html = fetch_url(BASE_URL).decode("utf-8", errors="ignore")
    articles = re.split(r"(?=<article\b)", html)
    rows = []
    current_category = None
    for article in articles:
        h2 = re.search(r"<h2>(.*?)</h2>", article, re.S)
        if h2:
            text = re.sub(r"\s+", " ", h2.group(1)).strip()
            if text:
                current_category = text
        h5 = re.search(r'card-title linea[^>]*>(.*?)</h5>', article, re.S)
        pdf = re.search(
            r'href="(https://www\.ama\.laquila\.it/wp-content/uploads/[^"]+\.pdf)"',
            article,
        )
        if not (h5 and pdf):
            continue
        fermate = re.search(
            r"Fermate principali\s*</h6>\s*<p>\s*(.*?)\s*</p>", article, re.S
        )
        rows.append(
            {
                "linea": re.sub(r"\s+", " ", h5.group(1)).strip(),
                "categoria": current_category,
                "pdf_url": pdf.group(1),
                "fermate_principali": re.sub(r"\s+", " ", fermate.group(1)).strip()
                if fermate
                else "",
            }
        )
    return rows


def download_pdfs(lines_index):
    os.makedirs(PDF_DIR, exist_ok=True)
    for line in lines_index:
        fname = line["pdf_url"].rsplit("/", 1)[-1]
        path = os.path.join(PDF_DIR, fname)
        line["pdf_local_path"] = path
        if os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"  [cache] {line['linea']} -> {fname}")
            continue
        try:
            data = fetch_url(line["pdf_url"])
            with open(path, "wb") as f:
                f.write(data)
            print(f"  [ok]    {line['linea']} -> {fname}")
        except RuntimeError as e:
            print(f"  [FAIL]  {line['linea']} -> {e}")


def is_time_cell(value):
    if value is None:
        return False
    value = value.strip()
    return value == "-" or bool(TIME_RE.match(value))


def normalize_time(value):
    if value is None:
        return None
    value = value.strip().replace(":", ".").replace(",", ".")
    if value in ("", "-"):
        return None
    m = re.match(r"^(\d{1,2})\.(\d{2})$", value)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 27 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def extract_runs_from_pdf(pdf_path):
    """Estrae le singole corse (righe orario) da un PDF di linea.

    Ogni riga di tabella viene considerata una corsa se almeno il 40% delle
    celle (oltre la prima, che contiene la variante/codice corsa) assomiglia
    a un orario (HH.MM, HH:MM, HH,MM o '-' per "non transita").
    """
    runs = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row or len(row) < 2:
                            continue
                        body = row[1:]
                        time_like = [c for c in body if is_time_cell(c)]
                        nonempty_body = [c for c in body if c not in (None, "")]
                        if not time_like or not nonempty_body:
                            continue
                        if len(time_like) < len(nonempty_body) * 0.4:
                            continue
                        orari = [normalize_time(c) for c in body]
                        if not any(orari):
                            continue
                        variante = (row[0] or "").strip().replace("\n", " ")
                        runs.append({"variante_raw": variante, "orari": orari})
    except Exception as e:
        print(f"    errore parsing {pdf_path}: {e}")
        return runs, str(e)

    # le celle di variante che si estendono su più righe (rowspan) risultano
    # vuote nelle righe successive: propaghiamo l'ultimo valore noto
    last_variant = None
    for r in runs:
        if r["variante_raw"]:
            last_variant = r["variante_raw"]
        else:
            r["variante_raw"] = last_variant
    return runs, None


def parse_all(lines_index):
    schedules = []
    for line in lines_index:
        path = line.get("pdf_local_path")
        if not path or not os.path.exists(path):
            schedules.append({**line, "corse": [], "errore": "PDF non scaricato"})
            continue
        runs, error = extract_runs_from_pdf(path)
        entry = {**line, "corse": runs}
        if error:
            entry["errore"] = f"parsing fallito: {error}"
        elif not runs:
            entry["errore"] = (
                "nessuna corsa estratta (probabile PDF basato su immagine/scansione, "
                "richiede OCR)"
            )
        schedules.append(entry)
        print(f"  {line['linea']}: {len(runs)} corse estratte")
    return schedules


def export(schedules):
    os.makedirs(DATA_DIR, exist_ok=True)

    with open(os.path.join(DATA_DIR, "schedules.json"), "w", encoding="utf-8") as f:
        json.dump(schedules, f, ensure_ascii=False, indent=2)

    summary_path = os.path.join(DATA_DIR, "lines_summary.csv")
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["linea", "categoria", "num_corse", "prima_partenza", "ultimo_orario", "pdf_url", "note"]
        )
        for s in schedules:
            all_times = [t for run in s["corse"] for t in run["orari"] if t]
            writer.writerow(
                [
                    s["linea"],
                    s["categoria"],
                    len(s["corse"]),
                    min(all_times) if all_times else "",
                    max(all_times) if all_times else "",
                    s["pdf_url"],
                    s.get("errore", ""),
                ]
            )

    long_path = os.path.join(DATA_DIR, "schedules_long.csv")
    with open(long_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["linea", "categoria", "corsa_n", "variante", "posizione_fermata", "orario"])
        for s in schedules:
            for corsa_n, run in enumerate(s["corse"], start=1):
                for pos, orario in enumerate(run["orari"], start=1):
                    if orario:
                        writer.writerow(
                            [s["linea"], s["categoria"], corsa_n, run["variante_raw"], pos, orario]
                        )

    print(f"\nEsportato in {DATA_DIR}:")
    print("  - schedules.json (dati completi)")
    print("  - lines_summary.csv (riepilogo per linea)")
    print("  - schedules_long.csv (tabella completa di tutti gli orari)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true", help="usa i PDF già presenti")
    args = parser.parse_args()

    print("1. Scansione pagina elenco linee AMA...")
    lines_index = crawl_lines_index()
    print(f"   trovate {len(lines_index)} linee/orari")

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "lines_index.json"), "w", encoding="utf-8") as f:
        json.dump(lines_index, f, ensure_ascii=False, indent=2)

    if not args.skip_download:
        print("\n2. Download PDF...")
        download_pdfs(lines_index)
    else:
        for line in lines_index:
            fname = line["pdf_url"].rsplit("/", 1)[-1]
            line["pdf_local_path"] = os.path.join(PDF_DIR, fname)

    print("\n3. Estrazione orari dai PDF...")
    schedules = parse_all(lines_index)

    print("\n4. Esportazione tabelle...")
    export(schedules)


if __name__ == "__main__":
    main()
