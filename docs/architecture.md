# Architettura

AIM Voto è un monolite Django con template HTML e JavaScript locale. Gunicorn serve il web e WhiteNoise gli asset. Un processo Django separato elabora la coda e aggiorna le consultazioni programmate. Entrambi condividono PostgreSQL. In sviluppo il worker apre connessioni SMTP solo verso Mailpit o loopback e ammette mittente e destinatari dimostrativi `@example.invalid`. Il browser usa il sito Django e, nel percorso di voto, richiede JavaScript per scambiare il link personale con una sessione.

- `config` gestisce configurazione, accesso staff, MFA, limiti di richiesta e avvio.
- `members` valida i file e importa gli aventi diritto nella bozza selezionata. L'anteprima appartiene all'operatore e alla consultazione. La conferma usa lo stesso lock della consultazione usato per inserimento manuale e apertura. Una variazione dei dati rilevanti richiede una nuova conferma.
- `voting` gestisce consultazioni, credenziali, voto palese e segreto, partecipazione e scrutinio.
- `mailings` compone le anteprime, congela la selezione confermata e gestisce gli invii. PostgreSQL è anche la coda persistente, sufficiente per il primo rilascio e monitorabile con gli strumenti del database.

## Stati e scadenze

Lo staff nazionale configura e modifica la bozza. Può aprirla subito, programmarla, oppure eliminarla finché è una bozza. Può riportare in bozza una consultazione programmata, aprirla subito o annullarla. Alla prima apertura si creano le credenziali e si congelano la platea, le opzioni, i documenti e le regole. La correzione dei recapiti modifica solo lo snapshot di contatto dell'elettore e produce un audit.

La consultazione aperta può passare a chiusa o annullata. Lo staff può pubblicare lo scrutinio di una consultazione chiusa oppure annullarla con motivazione. Le consultazioni chiuse, scrutinate e annullate non si riaprono nel percorso ordinario. Il worker chiude le consultazioni scadute e annulla una programmazione interamente trascorsa senza apertura. Nel database gli istanti sono confrontabili in UTC. L'interfaccia presenta l'ora Europe/Rome e rifiuta orari locali ambigui o inesistenti al cambio dell'ora.

## Accettazione della scheda

La schermata di riepilogo invia un payload firmato che associa risposte, ID della richiesta, revisione attesa e sessione elettorale. Il payload è temporaneamente nel browser dell'elettore. Il server prende il lock della consultazione, controlla la posizione e la versione della credenziale, convalida le risposte e cerca la ricevuta della richiesta. Una ricevuta esistente indica anche se quella revisione è ancora corrente. Il rifiuto di una revisione superata impone una nuova conferma.

Per una nuova registrazione, il punto di accettazione è la lettura di `clock_timestamp()` immediatamente prima della scrittura, sotto lo stesso lock usato per chiudere la consultazione. L'ora reale dopo l'attesa del lock rende efficace la scadenza anche con il worker fermo. Scheda e ricevuta sono salvate nella stessa transazione, che deve concludersi prima della risposta di successo. Un retry non riapplica una scheda precedente. Il lock è per consultazione. La capacità sotto accessi simultanei va misurata su un ambiente rappresentativo.

## Invii

Le campagne conservano il modello e le posizioni verificate, comprese le esclusioni. Conferma e worker ricontrollano la platea, lo stato di voto per i solleciti, lo stato della consultazione, l'intervallo e il recapito. Il worker acquisisce una posizione, controlla la credenziale attiva e registra il passaggio a “In invio” in PostgreSQL. Dopo il commit comunica con SMTP. Un'altra transazione registra l'esito. Lo stato “In invio” abbandonato diventa “Esito incerto”. Gli errori temporanei con rifiuto certo possono tornare in coda con attesa crescente. Lo stato SMTP ambiguo richiede verifica operativa. Tra commit di acquisizione e risposta SMTP esiste una finestra in cui una chiusura, una revoca o un voto possono intervenire mentre il messaggio è già in lavorazione.

## Presenze e deleghe

La conferma della presenza è esplicita e genera un registro autonomo. Richieste, approvazioni e revoche di delega condividono il lock della consultazione con la registrazione del voto. L’approvazione controlla appartenenza alla platea, assenza di una scheda diretta, assenza di catene di deleghe e numero di deleghe già assegnate al delegato. La soglia applicata è tre deleghe fino a 500 aventi diritto nella platea congelata e cinque oltre 500.

La sessione del delegato può confermare la scheda della posizione rappresentata solo con una delega approvata. La chiave univoca della scheda resta la posizione del delegante. Lo scrutinio usa quindi le stesse regole di conteggio del voto diretto. Il payload di conferma firmato identifica la delega e la versione della credenziale del delegato. La scheda nominativa conserva la posizione che ha registrato il voto per delega.

## Voto segreto

Il server salva in una transazione la ricevuta di partecipazione e la scheda cifrata. La ricevuta identifica la posizione elettorale e, per una delega, chi ha votato per suo conto. La scheda cifrata ha UUID casuale e come unico riferimento la consultazione. La sua lunghezza è uniforme per tutte le schede della stessa votazione. La ricevuta e l’audit sono indipendenti dalle preferenze.

Dopo la chiusura, lo scrutinio decifra le schede in memoria e confronta il numero di schede e quello delle deleghe esercitate con le ricevute prima di pubblicare. I report del voto segreto usano il totale per opzione e l’elenco degli aventi diritto. Nei report del voto palese le preferenze della scheda corrente sono attribuite all’elettore rappresentato, con il nome del delegato quando presente.
