# Locationsforappdev
This repo contains a single json file with random locations to test the google maps api around Abruzzi/Italy

## Crawler orari linee AMA L'Aquila

`ama_orari_crawler.py` scarica dal sito di [AMA L'Aquila](https://www.ama.laquila.it/linee-e-orari/elenco-linee-ama/)
i PDF degli orari di tutte le linee (urbane, festive, scolastiche) e ne
estrae le tabelle di corse in dati strutturati, inclusi i nomi delle
fermate: nei PDF originali sono impaginati con testo ruotato di 90°
(una parola per colonna, letta verticalmente), quindi vengono ricostruiti
raggruppando i caratteri per colonna della tabella invece di affidarsi
all'estrazione testuale "piatta" di pdfplumber, che li restituirebbe in
ordine sbagliato.

Uso:
```
pip install pdfplumber
python3 ama_orari_crawler.py
```

I risultati vengono scritti in `data/ama_orari/`:
- `lines_index.json` — elenco linee con categoria, itinerario e link al PDF
- `schedules.json` — dati completi per ogni linea: una o piu' "tabelle"
  (es. andata/ritorno), ciascuna con l'elenco delle fermate e le corse
- `lines_summary.csv` — riepilogo per linea (numero corse, primo/ultimo orario)
- `schedules_long.csv` — tabella con tutti gli orari, una riga per ogni
  passaggio a fermata
  (`linea, categoria, tabella_n, corsa_n, variante, posizione_fermata, fermata, orario`)

I PDF scaricati (`data/ama_orari/pdf/`) non sono versionati: vengono
riscaricati automaticamente ad ogni esecuzione (con cache locale e retry,
perché il sito AMA a volte chiude la connessione).

Limiti noti:
- 3 dei 46 PDF trovati (legenda linee, "punti di interesse", notturno bus)
  sono immagini scansionate senza livello testuale e non vengono estratti.
- Nomi delle fermate non ricostruiti per la linea "2" (percorso circolare
  con intestazione multi-riga non allineata alla griglia delle colonne) e
  per una delle 3 tabelle della linea "7" e della "Navetta Ex-Caserma
  Rossi"; gli orari restano corretti, mancano solo le etichette di colonna.
- Un paio di PDF (es. M12X) impaginano la tabella "ruotata" (una riga per
  fermata, una colonna per corsa anziché il contrario): gli orari estratti
  sono corretti ma l'etichetta di riga mostra il nome fermata anziché il
  codice corsa.

## Refactor feed GTFS con i nuovi orari

`gtfs_refactor.py` prende un vecchio export GTFS (routes/stops/trips/
stop_times.csv, es. mancante delle linee A e B) e lo rigenera usando gli
orari freschi estratti da `ama_orari_crawler.py`.

Uso:
```
python3 gtfs_refactor.py --old-gtfs-dir /percorso/al/vecchio/gtfs \
    --schedules data/ama_orari/schedules.json --out-dir data/gtfs_new
```

Il risultato (già presente in `data/gtfs_new/`) contiene 42 linee (le
uniche 42 delle 46 trovate dal crawler con almeno una corsa estratta) e
810 corse rigenerate da zero. route_id vengono riusati quando il
route_short_name coincide col vecchio feed (es. "1", "11A", "12A", ...);
linee mai censite prima come "A" e "B" ottengono un nuovo route_id.

**Limite principale: le coordinate delle fermate.** I PDF degli orari non
contengono lat/lon, quindi le fermate vengono associate al vecchio
`stops.csv` solo per nome esatto (normalizzato). Il vecchio feed nomina le
fermate in modo molto più granulare e specifico per direzione ("VIA
STRINELLA fronte Parco Unicef" vs "lato Parco Unicef" come fermate
distinte), mentre i PDF usano nomi generici ("Via Strinella"): il feed
rigenerato riesce a recuperare le coordinate solo per ~5 fermate su 253
(soprattutto capolinea come "Terminalbus", "Fontana Luminosa",
"L'Aquilone"). Le altre ~250 fermate sono scritte con `stop_lat`/`stop_lon`
vuoti e `need_geocoding=1` (colonna aggiunta rispetto allo standard GTFS)
e vanno geolocalizzate a mano o con un servizio di geocoding — provare ad
abbinarle per prefisso/sottostringa invece che per nome esatto è stato
scartato perché il vecchio feed usa spesso "lato X" / "fronte X" per
indicare fermate sui due lati opposti della strada: un match approssimato
rischierebbe di assegnare coordinate sbagliate.

shapes.csv non viene toccato/rigenerato: nessuna geometria di percorso è
ricavabile dai PDF, quindi le nuove corse hanno `shape_id` vuoto.
