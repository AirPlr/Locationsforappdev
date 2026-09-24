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
from collections import defaultdict

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


def is_data_row(row):
    """Una riga di tabella e' una corsa se >=40% delle celle (oltre la
    prima, che contiene la variante/codice corsa) assomiglia a un orario."""
    if not row or len(row) < 2:
        return False
    body = row[1:]
    time_like = [c for c in body if is_time_cell(c)]
    nonempty_body = [c for c in body if c not in (None, "")]
    if not time_like or not nonempty_body:
        return False
    return len(time_like) >= len(nonempty_body) * 0.4


def canonical_columns(table):
    """Ricostruisce la griglia di colonne 'fine' dell'intera tabella: alcune
    righe uniscono celle adiacenti (es. una nota che occupa più colonne
    orario), quindi l'unione dei bordi di tutte le righe è più affidabile
    dei bordi di una singola riga."""
    edges = set()
    for row in table.rows:
        for cell in row.cells:
            if cell:
                edges.add(round(cell[0], 2))
                edges.add(round(cell[2], 2))
    edges = sorted(edges)
    return list(zip(edges, edges[1:]))


def column_index(x, columns):
    for i, (x0, x1) in enumerate(columns):
        if x0 - 1 <= x <= x1 + 1:
            return i
    return None


def extract_header_labels(page, table, header_row_idx, columns):
    """Ricostruisce il testo delle intestazioni di colonna, anche quando nel
    PDF sono impaginate ruotate di 90° (una parola per colonna, letta
    dall'alto verso il basso). I caratteri del PDF sono già nell'ordine di
    lettura corretto nel flusso del documento: basta raggrupparli per
    colonna senza riordinarli per posizione, che invertirebbe il testo."""
    cells = [c for c in table.rows[header_row_idx].cells if c]
    if not cells:
        return None
    top = min(c[1] for c in cells)
    bottom = max(c[3] for c in cells)
    buckets = defaultdict(list)
    for ch in page.chars:
        if top - 3 <= ch["top"] and ch["bottom"] <= bottom + 3:
            idx = column_index(ch["x0"], columns)
            if idx is not None:
                buckets[idx].append(ch["text"])
    labels = []
    for i in range(len(columns)):
        text = re.sub(r"\s+", " ", "".join(buckets.get(i, []))).strip()
        labels.append(text)
    return labels


def looks_like_prose(labels):
    """Scarta come intestazione un testo che e' in realta' una frase (es. la
    riga "Itinerario: ...", tutta in un'unica cella) invece di brevi nomi di
    fermata distribuiti su piu' colonne."""
    non_empty = [l for l in labels if l]
    if not non_empty:
        return True
    if any(len(l) > 80 for l in non_empty):
        return True
    if len(non_empty) <= 2 and sum(len(l) for l in non_empty) > 100:
        return True
    return False


def extract_tabelle_from_pdf(pdf_path):
    """Estrae da un PDF di linea una o piu' 'tabelle' (es. andata/ritorno),
    ciascuna con l'elenco delle fermate (quando ricostruibile) e le corse
    con i relativi orari, allineati posizionalmente alle fermate."""
    tabelle = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.find_tables()
                last_header_candidate = None
                for table in tables:
                    text_rows = table.extract()
                    data_indices = [i for i, row in enumerate(text_rows) if is_data_row(row)]
                    if not data_indices:
                        if text_rows and len(text_rows) <= 3:
                            last_header_candidate = (table, len(text_rows) - 1)
                        continue

                    columns = canonical_columns(table)
                    fermate = None
                    # la riga di intestazione e' di solito subito sopra la prima
                    # corsa, ma a volte e' separata da righe vuote residue
                    # (artefatti della griglia): risaliamo fino alla prima riga
                    # non vuota.
                    header_idx = None
                    for cand in range(data_indices[0] - 1, max(data_indices[0] - 6, -1), -1):
                        if any(c not in (None, "") for c in text_rows[cand]):
                            header_idx = cand
                            break
                    if header_idx is not None:
                        labels = extract_header_labels(page, table, header_idx, columns)
                        if labels and not looks_like_prose(labels):
                            fermate = labels[1:]
                    if fermate is None and last_header_candidate is not None:
                        h_table, h_idx = last_header_candidate
                        h_columns = canonical_columns(h_table)
                        labels = extract_header_labels(page, h_table, h_idx, h_columns)
                        if labels and not looks_like_prose(labels):
                            fermate = labels[1:]

                    corse = []
                    last_variant = None
                    for i in data_indices:
                        row = text_rows[i]
                        variant = (row[0] or "").strip().replace("\n", " ")
                        # una cella di variante molto lunga e' quasi certamente
                        # un elenco di fermate finito nella colonna sbagliata
                        # (celle unite su piu' righe), non un vero codice corsa
                        if len(variant) > 40:
                            variant = ""
                        if variant:
                            last_variant = variant
                        orari = [normalize_time(c) for c in row[1:]]
                        if not any(orari):
                            continue
                        corse.append({"variante_raw": variant or last_variant, "orari": orari})
                    if not corse:
                        continue

                    max_len = max(len(c["orari"]) for c in corse)
                    if fermate:
                        fermate = (fermate + [""] * max_len)[:max_len]
                    tabelle.append({"fermate": fermate, "corse": corse})
                    last_header_candidate = None
    except Exception as e:
        print(f"    errore parsing {pdf_path}: {e}")
        return tabelle, str(e)
    return tabelle, None


def parse_all(lines_index):
    schedules = []
    for line in lines_index:
        path = line.get("pdf_local_path")
        if not path or not os.path.exists(path):
            schedules.append({**line, "tabelle": [], "errore": "PDF non scaricato"})
            continue
        tabelle, error = extract_tabelle_from_pdf(path)
        entry = {**line, "tabelle": tabelle}
        num_corse = sum(len(t["corse"]) for t in tabelle)
        num_con_fermate = sum(1 for t in tabelle if t["fermate"])
        if error:
            entry["errore"] = f"parsing fallito: {error}"
        elif not tabelle:
            entry["errore"] = (
                "nessuna corsa estratta (probabile PDF basato su immagine/scansione, "
                "richiede OCR)"
            )
        print(
            f"  {line['linea']}: {num_corse} corse in {len(tabelle)} tabelle "
            f"({num_con_fermate} con nomi fermata)"
        )
        schedules.append(entry)
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
            all_times = [t for tab in s["tabelle"] for run in tab["corse"] for t in run["orari"] if t]
            num_corse = sum(len(tab["corse"]) for tab in s["tabelle"])
            writer.writerow(
                [
                    s["linea"],
                    s["categoria"],
                    num_corse,
                    min(all_times) if all_times else "",
                    max(all_times) if all_times else "",
                    s["pdf_url"],
                    s.get("errore", ""),
                ]
            )

    long_path = os.path.join(DATA_DIR, "schedules_long.csv")
    with open(long_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["linea", "categoria", "tabella_n", "corsa_n", "variante", "posizione_fermata", "fermata", "orario"]
        )
        for s in schedules:
            for tabella_n, tab in enumerate(s["tabelle"], start=1):
                fermate = tab["fermate"] or []
                for corsa_n, run in enumerate(tab["corse"], start=1):
                    for pos, orario in enumerate(run["orari"], start=1):
                        if orario:
                            fermata = fermate[pos - 1] if pos - 1 < len(fermate) else ""
                            writer.writerow(
                                [
                                    s["linea"],
                                    s["categoria"],
                                    tabella_n,
                                    corsa_n,
                                    run["variante_raw"],
                                    pos,
                                    fermata,
                                    orario,
                                ]
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
