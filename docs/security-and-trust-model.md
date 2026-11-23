# Sicurezza e modello di fiducia

## Voto nominativo

Il sistema associa la scheda corrente alla posizione elettorale. Il link personale è una credenziale al portatore e chi lo possiede può accedere come il socio nel perimetro e nella finestra di quella consultazione. Un amministratore autorizzato a recuperarlo ha la stessa possibilità. Conferma esplicita e audit aiutano a ricostruire le operazioni. Gli amministratori del database possono leggere o alterare schede e log locali anche quando l'interfaccia applicativa nasconde i risultati temporaneamente.

Aprire il link avvia l'accesso e non costituisce voto o presenza assembleare. Il frammento dell'URL contiene la credenziale. Il browser la rimuove dalla barra degli indirizzi prima di inviarla via POST a Django, con CSRF. La sessione Django sul server resta legata alla posizione e alla versione della credenziale. La rigenerazione revoca il link e la versione precedente della sessione per nuovi invii, conservando la scheda già registrata.

I link risultano visibili nella casella di prova di Mailpit. Proteggi il browser, la posta e la chat WhatsApp usata per l'invio manuale. Per un invio assistito verifica identità e recapito prima di copiare un link, motiva la revoca quando è necessario sostituirlo e riferisci eventuali anomalie al responsabile tecnico.

## Controlli applicativi

Django gestisce password, verifica CSRF, cookie `HttpOnly` per la sessione e controllo dello staff. Il profilo di produzione impone HTTPS, cookie sicuri e TOTP. Gli account staff MVP hanno responsabilità nazionale. Le viste verificano l'appartenenza di elettori e risorse alla singola consultazione. Il backend convalida le schede e rifiuta la modalità segreta. Il limite per richieste sensibili usa contatori condivisi nei processi tramite PostgreSQL.

Django restituisce direttive anti-cache e limita il referrer alla stessa origine con `Referrer-Policy: same-origin`. Il browser trasmette soltanto l’URL senza il frammento. La policy sui contenuti limita asset e richieste allo stesso sito. Gunicorn lascia spenti i log di accesso. Il proxy di un eventuale pilota deve scartare i body, gli identificatori delle sessioni, query e percorsi contenenti credenziali, anche da analisi, errori e tracing. Mantieni disattivato il debug con qualsiasi dato operativo. I documenti allegati rientrano nei dati riservati e vanno verificati prima del caricamento.

## Custodia e rotazione delle chiavi

`DJANGO_SECRET_KEY`, `VOTING_LINK_KEYS` e credenziali del database hanno ruoli distinti. L'archivio dati conserva il digest SHA-256 indicizzato e un cifrato Fernet. `VOTING_LINK_KEYS` è una lista separata da virgole, con la chiave nuova al primo posto. I processi usano la prima per cifrare e la lista per decifrare. La perdita di tutte le chiavi usate per un cifrato impone la revoca e la rigenerazione del relativo link.

Per ruotare senza cambiare i collegamenti, fai un backup separato dei dati e delle chiavi. Pianifica la sospensione di web e worker fuori da una consultazione aperta. Genera una chiave con `Fernet.generate_key()` in un terminale privato e anteponila in `VOTING_LINK_KEYS` mantenendo le precedenti. Arresta i servizi prima di applicare e verificare la ricifratura.

```sh
docker compose stop web worker
docker compose run --rm --no-deps web python manage.py rotate_link_encryption --apply
docker compose run --rm --no-deps web python manage.py rotate_link_encryption
```

Il primo controllo applica la ricifratura, preservando digest e versione. Il secondo richiede che tutte le credenziali siano verificabili con la prima chiave. Solo a verifica riuscita rimuovi le chiavi dal profilo corrente, conservandole per i backup che ancora le richiedono. Riavvia web e worker e verifica un link di prova. In caso di compromissione, ruota le chiavi e valuta anche la revoca dei link già esposti. Ricifrare il database lascia validi i collegamenti già distribuiti.

## Operazioni per un pilota

- Definisci un hosting isolato con HTTPS, aggiornamenti, monitoraggio, gestione incidenti e rate limiting aggiuntivo al proxy. Imposta al proxy il limite del corpo delle richieste a 6 MiB. Limita i privilegi PostgreSQL e separa i ruoli di servizio da quelli operativi. Attiva `AIM_TRUST_HTTPS_PROXY` solo dietro un proxy che sostituisce gli header ricevuti dal client e impedisce l'accesso diretto al backend.
- Assegna account individuali, TOTP, procedure di recupero e revoca e verifica i ruoli tramite prove con utenti distinti. Proteggi le chiavi applicative, i fattori TOTP e i backup con approvazioni e accessi nominativi.
- Concorda durata della conservazione, regole della consultazione, recapiti e procedura per i risultati. Verifica il modello nominativo con l'associazione e gli incaricati alla protezione dei dati. L'applicazione lascia all'associazione le valutazioni giuridiche e assembleari.
- Prepara l'invio reale come modifica separata, con trasporto autenticato e cifrato, controllo degli indirizzi e un piano di gestione degli esiti. Questa versione accetta esclusivamente Mailpit o un SMTP locale configurato per prove con `@example.invalid`.
- Valida carico, backup e ripristino, verifica la riconciliazione delle ricevute a seguito di un incidente. Predisponi un'esportazione del registro verso un sistema indipendente ad accesso ristretto, preservando l'assenza di token e preferenze. Il registro PostgreSQL locale è modificabile da chi controlla il database.

Riferimento sulla gestione delle chiavi e sulla cifratura dei dati. [OWASP Cryptographic Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
