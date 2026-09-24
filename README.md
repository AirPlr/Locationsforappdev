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
