# CLAUDE.md — Amazon Monitor (Beyblade X)

## Contesto del progetto

Raffaele colleziona Beyblade X Takaratomy e compra da Amazon Japan. Con l'aumento della domanda, scalper professionisti (con bot automatici) comprano tutto lo stock appena disponibile, spesso prima ancora che i preordini siano accessibili manualmente. L'obiettivo di questo progetto **non è fare scalping**, ma **competere ad armi pari**: sapere appena un prodotto è disponibile, con la stessa rapidità di chi usa automazione.

Il sistema NON fa acquisti automatici (no login Amazon, zero rischio ban account) — solo monitoring + notifica. L'acquisto resta manuale.

## Stato attuale

App Flask + SQLite + worker in background, già completa e testata **in sandbox** (logica, parsing, API REST — tutto verificato con unit test e chiamate curl). **Non ancora verificata su rete reale/locale dell'utente** — l'utente riporta che il comando per avviarla dà errori, ma non ha specificato quale comando o quale errore esatto.

**Prima cosa da fare: far girare `python3 app.py` nella cartella del progetto, leggere l'errore completo (traceback) e risolverlo.** Cause probabili da controllare per prime:
- Versione Python (serve 3.10+, controllare con `python3 --version`)
- `pip3 install -r requirements.txt` non eseguito o fallito silenziosamente
- Porta 5050 già occupata da un altro processo
- Permessi di scrittura nella cartella per creare `data.db` (SQLite)
- Eventuali differenze ambiente macOS (es. necessità di `python3` vs `python`)

## Architettura

```
amazon-monitor-app/
├── app.py              # Flask app, API REST, avvia il worker all'avvio
├── db.py                # Schema SQLite + helper di accesso dati
├── worker.py             # Loop di polling in background (thread separato)
├── scraper.py            # Costruzione URL Amazon, fetch pagina, parsing HTML
├── notifier.py            # Invio notifiche Telegram (Pushover predisposto ma non attivo)
├── marketplaces.py         # Domini + seller ID Amazon per JP/IT/FR/DE/UK/US
├── static/index.html        # Pannello web — vanilla JS, NIENTE build tool (no npm/React build)
├── requirements.txt
└── data.db              # creato automaticamente al primo avvio (SQLite, non versionare)
```

Un solo processo: Flask serve sia le API che il pannello statico; il worker di polling gira in un thread `daemon=True` nello stesso processo, avviato in `app.py` con `start_worker_thread()`. Guardia contro il doppio avvio del worker quando Flask è in modalità reload (`WERKZEUG_RUN_MAIN`).

## Decisioni tecniche prese (e perché)

- **SQLite invece di JSON**: scritture concorrenti sicure tra web panel e worker (WAL mode), e lo storico ASIN già notificati sopravvive ai riavvii — punto chiave per evitare notifiche duplicate.
- **Un solo processo invece di due** (web + worker separati): più semplice da avviare e gestire per uso personale, nessun bisogno di orchestrazione.
- **Pannello in vanilla JS, non React buildato**: zero dipendenze da Node/npm, basta `python3 app.py` e il browser. Il mockup originale era in React (mostrato come anteprima di design), ma l'implementazione reale è stata riscritta in HTML+JS puro servito da Flask per restare "gratis e semplice".
- **Filtro venditore "solo Amazon"**: applicato via parametro URL `&emi={seller_id}` direttamente nella ricerca Amazon (non in fase di parsing), così i risultati restituiti sono già garantiti Amazon, non serve indovinare dal markup della pagina prodotto.
- **Anti falsi-positivi**: il parser scarta esplicitamente risultati senza ASIN o senza titolo (meglio perdere un ciclo che notificare dati sporchi); ogni ASIN notificato viene salvato in `seen_products` con chiave `(source_type, source_id, marketplace, asin)` e non viene mai ri-notificato.
- **"Novità"** usa il sort ufficiale Amazon `s=date-desc-rank` (affidabile, è un parametro documentato).
- **"Offerte"** è euristico: rileva badge sconto nell'HTML (classi tipo `.s-coupon-highlight-color`, `.a-badge-text`, pattern regex su "%"/"deal"/"offerta"/"sconto"). Amazon può cambiare il markup in qualsiasi momento — se in uso reale risulta troppo silenzioso o rumoroso, va rivisto in `scraper.py` → `_has_deal_badge()`.
- **Nessun login Amazon nello script**: il monitoring legge solo pagine di ricerca pubbliche, zero rischio ban account. L'auto-buy è stato esplicitamente escluso da questa fase per lo stesso motivo (richiederebbe sessione autenticata).

## Modello dati (SQLite)

- `monitors`: monitor creati manualmente dall'utente (keyword o URL diretta, marketplace selezionati, filtro venditore, tipo ricerca, enabled/disabled)
- `bundles`: pacchetti predefiniti attivabili con un toggle (3 di default: "Scopri Novità Beyblade X", "Takaratomy — Solo Amazon JP", "Offerte Beyblade X" — quest'ultimo disabilitato di default)
- `seen_products`: dedup storico per evitare doppie notifiche
- `logs`: log applicativo mostrato nella dashboard (ultimi 300 record)
- `settings`: token/chat ID Telegram, intervallo di polling in secondi (default 60)

## API REST già implementate

```
GET    /api/marketplaces
GET    /api/monitors          POST /api/monitors
PUT    /api/monitors/<id>     DELETE /api/monitors/<id>
GET    /api/bundles           PUT /api/bundles/<id>
GET    /api/settings          PUT /api/settings
POST   /api/settings/test-telegram
GET    /api/logs?limit=N
GET    /api/stats
```

## Marketplace supportati (dominio + seller ID Amazon ufficiale)

| Codice | Dominio | Seller ID |
|---|---|---|
| JP | amazon.co.jp | AN1VRQENFRJN5 |
| IT | amazon.it | APJ6JRA9NG5V4 |
| FR | amazon.fr | A1X6FK5RDHNB96 |
| DE | amazon.de | A3JWKAKR8XB7XF |
| UK | amazon.co.uk | A3P5ROKL5A1OLE |
| US | amazon.com | ATVPDKIKX0DER |

## Cosa è stato testato e cosa no

✅ Testato (in sandbox cloud): sintassi di tutti i file, tutte le API REST via curl (CRUD completo), parsing HTML con dati simulati (corretto scarto di risultati senza ASIN, corretto filtro "deals"), costruzione URL con parametri corretti, worker non va in crash anche con errori di rete.

⚠️ Non testato: fetch reale di Amazon dalla rete dell'utente. Dal sandbox cloud Amazon risponde 403 (IP da datacenter, bloccato dall'anti-bot) — comportamento atteso e diverso da quello che dovrebbe avere un IP residenziale italiano normale. **Questo va verificato per primo.**

## Prossimi passi possibili (non ancora implementati)

- Notifiche Pushover (interfaccia già predisposta come commento in `notifier.py`)
- Deploy permanente su VPS/Oracle Cloud Free Tier per farlo girare 24/7 (l'utente sta già configurando accesso remoto Mac via Tailscale + Termius per altri progetti, potrebbe voler fare lo stesso qui)
- Eventuale gestione retry/backoff più sofisticata se Amazon blocca con 403/503 frequenti

## Stile di lavoro preferito da Raffaele

Implementazioni complete e pronte all'uso, preservare i pattern architetturali esistenti, terminale e analisi errori da output reali (non ipotetici). È uno sviluppatore Senior iOS (Swift/UIKit/SwiftUI, Flutter secondario), quindi commenti tecnici concisi vanno bene, non serve spiegare concetti di programmazione di base.
