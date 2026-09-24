# Locationsforappdev
This repo contains a single json file with random locations to test the google maps api around Abruzzi/Italy

## Crawler orari linee AMA L'Aquila

`ama_orari_crawler.py` scarica dal sito di [AMA L'Aquila](https://www.ama.laquila.it/linee-e-orari/elenco-linee-ama/)
i PDF degli orari di tutte le linee (urbane, festive, scolastiche) e ne
estrae le tabelle di corse in dati strutturati.

Uso:
```
pip install pdfplumber
python3 ama_orari_crawler.py
```

I risultati vengono scritti in `data/ama_orari/`:
- `lines_index.json` — elenco linee con categoria, itinerario e link al PDF
- `schedules.json` — dati completi (linea, corse, orari) per ogni linea
- `lines_summary.csv` — riepilogo per linea (numero corse, primo/ultimo orario)
- `schedules_long.csv` — tabella con tutti gli orari, una riga per ogni
  passaggio a fermata (`linea, categoria, corsa_n, variante, posizione_fermata, orario`)

I PDF scaricati (`data/ama_orari/pdf/`) non sono versionati: vengono
riscaricati automaticamente ad ogni esecuzione (con cache locale e retry,
perché il sito AMA a volte chiude la connessione).

Nota: 3 dei 46 PDF trovati (legenda linee, "punti di interesse", notturno
bus) sono immagini scansionate senza livello testuale e non vengono estratti.
