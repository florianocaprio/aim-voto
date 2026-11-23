# AIM Voto

AIM Voto gestisce consultazioni online per Angeli in Moto ODV con voto **Palese (nominativo)** o **Segreto**, presenze e deleghe. Nel palese i PDF riportano i votanti e le loro scelte. Nel segreto il database conserva separatamente la partecipazione e le schede cifrate. I PDF riportano gli aventi diritto e i risultati aggregati. L'ambiente di sviluppo usa esclusivamente anagrafiche sintetiche e Mailpit.

## Avvio locale

Servono Docker con Compose v2 o successivo e Python 3 per generare i segreti. Esegui i comandi nella radice del progetto.

```sh
python3 scripts/setup_local.py
docker compose up --build
```

Il primo comando crea `.env` con permessi 0600, genera segreti distinti e conserva l'eventuale file già presente. Eseguilo una volta sola per ambiente. Compose attende PostgreSQL, applica le migrazioni, raccoglie gli asset statici e avvia il web e il worker.

L’applicazione usa la porta 8000 e Mailpit la porta 8025, entrambe su `127.0.0.1`. PostgreSQL e SMTP sono raggiungibili all’interno della rete di Compose. Usa lo stesso host configurato in `AIM_BASE_URL` quando condividi un link tra finestre locali. [AIM Voto](http://localhost:8000) · [Mailpit](http://localhost:8025)

Crea l’account amministrativo da un secondo terminale.

```sh
docker compose exec web python manage.py create_staff responsabile-demo
```

Digita due volte una password unica di almeno 14 caratteri. L’inserimento è mascherato e Django conserva l’hash della password. Usa l’account creato per accedere alla gestione. [Accesso amministrativo](http://localhost:8000/account/accesso/)

Per provare MFA, configura il dispositivo in un terminale privato, imposta `REQUIRE_ADMIN_MFA=1` in `.env` e ricrea il web.

```sh
docker compose exec web python manage.py enroll_staff_totp responsabile-demo
docker compose up -d web
```

L'app di autenticazione accetta la chiave alfanumerica con parametri TOTP a 6 cifre, SHA-1 e intervallo di 30 secondi. Tratta la chiave come una credenziale. La sostituzione con `--replace` invalida il fattore precedente e richiede conferma. Il profilo di produzione impone il secondo fattore a ogni account staff.

## Scenario dimostrativo

1. Crea una consultazione con chiusura futura. Nella bozza apri **Aventi diritto**. Con **Aggiungi elettore** compili nome, cognome, matricola ed email. Per la prova con tre posizioni, scegli **Importa CSV**, scarica il **CSV con tre elettori dimostrativi**, caricalo e conferma l’anteprima. In alternativa, esegui `docker compose exec web python manage.py seed_demo ID_CONSULTAZIONE` con l’ID numerico della bozza.
2. Aggiungi un quesito a scelta singola. Controlla la platea e l’anteprima della scheda, poi apri la consultazione. Seleziona **Prepara inviti email** e conferma l’accodamento.
3. In Mailpit, apri gli inviti di due soci in contesti separati del browser. Registra due schede e poi sostituisci il voto di uno dei due. Nel pannello **Elettori della votazione** prova **Copia link personale** e confronta il collegamento con quello del relativo invito.
4. Apri **Invia sollecito ai non votanti**, controlla che l'unico destinatario sia il terzo socio e conferma. Attendi un solo nuovo messaggio in Mailpit, con lo stesso link personale del primo invito del terzo socio.
5. Chiudi la consultazione e pubblica lo scrutinio. Verifica due votanti e due schede correnti. I conteggi usano l'ultima scelta del socio che ha sostituito il voto.

Inserisci soltanto indirizzi `@example.invalid` nelle prove di invio. Il worker utilizza Mailpit e distingue accettazione SMTP, esclusione, errore ed esito incerto. Per provare la versione mobile usa l'emulazione del browser locale.

## Verifiche riproducibili

```sh
docker compose config --quiet
docker compose --profile test run --build --rm test
```

Il servizio di test esegue Ruff, il controllo Django, la verifica delle migrazioni e la suite su un database di test PostgreSQL separato. Usa un progetto Compose indipendente per evitare di mescolare la posta generata dai test con la dimostrazione.

```sh
AIM_MAILPIT_PORT=18025 docker compose -p aim-voto-test --profile test run --build --rm test
AIM_MAILPIT_PORT=18025 docker compose -p aim-voto-test down
```

La CI esegue i medesimi controlli e analizza la cronologia Git con Gitleaks. `docker compose down` arresta i servizi locali e conserva il database.

## Verifiche eseguite per il primo MVP

- La procedura Compose ha avviato l'applicazione, PostgreSQL, il worker e Mailpit. Ruff, il controllo Django, la verifica delle migrazioni e tutti i 49 test su PostgreSQL sono passati.
- In Chrome sono stati importati tre soci sintetici e ricevuti i tre inviti in Mailpit. Dopo due voti e una sostituzione, il sollecito ha prodotto un solo nuovo messaggio con il link personale già assegnato al terzo socio. Lo scrutinio ha contato due votanti e due schede correnti, usando la scelta sostitutiva. Sono state verificate le esportazioni PDF e CSV.
- La copia del link negli appunti è stata verificata in Chrome. L'interfaccia elettore è stata provata con l'emulazione di iPhone 13 e Pixel 7. Restano da eseguire le prove su telefoni fisici e il collaudo di carico fino a 2.000 elettori.
- Il dump dimostrativo è stato ripristinato in un database PostgreSQL separato. Il ripristino ha conservato tre soci, tre posizioni elettorali, due schede correnti e tre ricevute, con revisione massima 2.
- Dopo l’aggiunta di presenze e deleghe, la suite PostgreSQL è passata con 58 test, compresi il cambio di soglia a 501 aventi diritto e le approvazioni concorrenti. Il percorso da conferma presenza a voto per delega è stato provato in Chrome con contesti separati.
- Con la platea gestita per consultazione, la suite è passata con 62 test. In Chrome sono stati verificati l’inserimento manuale, l’importazione di un CSV a quattro colonne e la consultazione dello storico con la stessa matricola e recapiti distinti.

## Versioni e dipendenze

Python 3.13.18 e Django 5.2.19 LTS offrono release di sicurezza supportate. PostgreSQL 17.12 garantisce le transazioni e i blocchi di riga usati dall'applicazione. Django serve i template, Gunicorn gestisce il web e WhiteNoise pubblica gli asset locali. Il worker legge la coda persistente del database e consegna a Mailpit 1.34.0. Le immagini sono bloccate per versione e digest, le dipendenze Python per versione e hash.

Per aggiornare i file bloccati parti dai corrispondenti `.in` e prova l'aggiornamento con Compose. Usa il compilatore uv 0.13.9.

```sh
uv --no-cache pip compile --universal --python-version 3.11 --generate-hashes requirements.in -o requirements.txt
uv --no-cache pip compile --universal --python-version 3.11 --generate-hashes requirements-dev.in -o requirements-dev.txt
```

- [Versioni supportate di Django](https://www.djangoproject.com/download/)
- [Supporto PostgreSQL](https://www.postgresql.org/support/versioning/)
- [Release di Python 3.13.18](https://www.python.org/downloads/release/python-31318/)
- [Release Mailpit 1.34.0](https://github.com/axllent/mailpit/releases/tag/v1.34.0)

## Documentazione

- [Requisiti](docs/requirements.md)
- [Architettura](docs/architecture.md)
- [Modello dati](docs/data-model.md)
- [Guida amministrativa](docs/admin-guide.md)
- [Sicurezza e modello di fiducia](docs/security-and-trust-model.md)
- [Backup e ripristino](docs/backup-restore.md)
- [Roadmap](docs/roadmap.md)
- [Segnalazioni di sicurezza](SECURITY.md)

Il Compose è destinato alla prova locale. Prima di usare dati reali servono la preparazione del pilota e le verifiche descritte nella documentazione.

## Licenza

La licenza del codice originale è da definire. Le dipendenze mantengono le rispettive licenze.
