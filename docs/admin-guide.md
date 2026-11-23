# Guida amministrativa

L’ambiente locale di AIM Voto è destinato a prove con soci sintetici. Le email indirizzate a recapiti `@example.invalid` sono raccolte in Mailpit. I comandi per l’avvio, la creazione dell’account nazionale e l’attivazione della verifica TOTP sono nel [README](../README.md).

Con un account nazionale puoi preparare consultazioni per qualsiasi sezione dell’associazione. Verifica titolo, platea e scadenza prima di confermare ogni apertura o campagna.

## Preparare la platea degli aventi diritto

Nella bozza apri **Aventi diritto**. Con **Aggiungi elettore** inserisci nome, cognome, matricola ed email. Cellulare e sezione sono facoltativi. In bozza puoi modificare o rimuovere una posizione dal relativo dettaglio.

1. Apri **Importa CSV** nella platea. Scarica il modello CSV o XLSX. Per la dimostrazione puoi scaricare il **CSV con tre elettori dimostrativi**.
2. Carica il file. Per il CSV scegli separatore e codifica. Sono disponibili punto e virgola, virgola e tabulazione, con UTF-8 anche con BOM, Windows-1252 o ISO-8859-1. Indica se la prima riga contiene le intestazioni.
3. Seleziona **Associa le colonne**. Associa matricola, nome, cognome ed email. Cellulare e sezione sono facoltativi. Nel modello XLSX i campi associati vanno memorizzati come testo, in particolare per conservare gli zeri iniziali. Viene letto il primo foglio.
4. Seleziona **Controlla l’anteprima**. Esamina nuovi soci, aggiornamenti, righe invariate, errori e avvisi. Correggi eventuali righe con errore e ricarica il file, oppure verifica l’associazione delle colonne.
5. Quando il riepilogo è corretto, seleziona la casella di verifica e premi **Conferma importazione**. Se i dati rilevanti cambiano prima del salvataggio, il sistema presenta una nuova anteprima da confermare.

Il file può contenere al massimo 5.000 righe dati e 32 colonne e deve rientrare in 2 MiB. La platea può contenere fino a 2.000 elettori. Per la matricola contano le maiuscole, le minuscole e gli zeri iniziali. Una matricola ripetuta nel file richiede una correzione. Una matricola già presente aggiorna la posizione di quella bozza e le posizioni assenti dal file restano nella platea. Un’email condivisa genera un avviso. Se una colonna facoltativa resta senza associazione, il valore esistente viene conservato. Una cella vuota in una colonna associata cancella il valore precedente.

L’anteprima è accessibile dal solo operatore che ha caricato il file e scade dopo 30 minuti. Il successivo caricamento elimina le anteprime scadute. Il comando `docker compose exec web python manage.py purge_imports` consente la pulizia anche senza avviare un’altra importazione.

## Preparare e aprire una consultazione

1. In **Consultazioni** scegli **Nuova consultazione**. Imposta titolo, descrizione e scadenza in ora di Roma. Indica l’ora di apertura se vuoi programmarla. Scegli se consentire la sostituzione della scheda, attiva inizialmente.
2. Da **Aggiungi quesito** inserisci una mozione, una scelta singola, una scelta multipla o una selezione di candidati. Per le selezioni multiple stabilisci il numero massimo di preferenze. **Gestisci documenti** accetta fino a cinque PDF da 3 MiB ciascuno.
3. Apri **Aventi diritto** e inserisci la platea di questa consultazione, a mano o con un file. Controlla l’elenco prima di aprire o programmare la votazione.
4. Controlla **Anteprima scheda**, aventi diritto e scadenza. Seleziona **Apri ora** oppure **Programma l’apertura** e conferma l’operazione. Dallo stato programmato puoi tornare in bozza per fare modifiche. Il worker applica gli orari programmati. Il sistema richiede un orario univoco nei giorni di cambio dell’ora legale.
5. Durante l’apertura, seleziona **Prepara inviti email**. Scegli la sezione o l’intera platea, personalizza oggetto e testo, verifica i destinatari e conferma l’accodamento.

Il voto è palese. All’apertura si congelano quesiti, opzioni, regole e platea. L’apertura e l’accodamento degli inviti richiedono conferme distinte.

Per consultare lo storico apri la votazione conclusa da **Consultazioni** e scegli **Elettori della votazione**. La stessa matricola può riportare un recapito diverso in un’altra consultazione.

## Assistere un elettore

In **Elettori della votazione**, cerca per nome, matricola, email o cellulare e apri il nome del socio. Puoi aprire la stessa scheda dal nome di un destinatario nello **Storico invii**. La scheda mostra i recapiti, l’ultimo invito o reinvio e la partecipazione.

- **Copia link personale** recupera durante l’apertura il collegamento attivo usato dalla coda di invio. Se il browser blocca la copia automatica, copia il valore dal campo selezionato.
- **Copia messaggio e apri WhatsApp Web** usa la sessione WhatsApp del browser e il cellulare della scheda elettore. Inserisci i numeri esteri con il prefisso internazionale; il sistema aggiunge `+39` ai cellulari italiani senza prefisso. Il messaggio rimane negli appunti. Incollalo nella chat, verifica il destinatario e premi **Invia**. Se il browser blocca l’apertura automatica, usa **Apri chat WhatsApp Web**. I link di un’installazione su `localhost` si aprono solo sul computer che la esegue.
- **Reinvia email** apre un’anteprima per il singolo socio. Verifica destinatario e testo prima di confermare. Il reinvio individuale segue la coda email e mantiene la stessa credenziale attiva.
- **Correggi recapiti** aggiorna solo l’email o il cellulare della posizione nella consultazione corrente. Verifica il nuovo recapito, seleziona la motivazione e salva. L’operazione è registrata e conserva persona e diritto di voto.
- **Revoca e rigenera link** richiede motivazione e conferma. Il nuovo link sostituisce il precedente e invalida le sessioni collegate alla versione precedente per nuovi invii. Una scheda già acquisita rimane corrente. Copia il nuovo link o usa il reinvio individuale dopo aver verificato il recapito.

Il link è una credenziale personale. Chi è autorizzato a copiarlo può tecnicamente usarlo per accedere come il socio. Copie, revoche e correzioni sono operazioni tracciate. [Modello di fiducia](security-and-trust-model.md).

## Presenze e deleghe

1. Nella bozza, seleziona **Consenti il voto per delega verificata dallo staff** se la consultazione prevede deleghe. La regola resta fissata all’apertura. Le consultazioni esistenti mantengono le deleghe disabilitate.
2. Dal link personale, il socio apre **Presenza e deleghe** e sceglie **Confermo la mia presenza**. L’accesso alla scheda, la conferma della presenza e l’acquisizione del voto sono tre azioni distinte. Per una consultazione aperta o già conclusa, lo staff trova l’elenco e **Scarica presenze CSV** nella sezione **Presenze e deleghe**.
3. Il socio che delega può ancora inoltrare la richiesta indicando l’**ID socio** di un altro avente diritto. In alternativa, chi ha ricevuto la delega apre **Presenta una delega ricevuta**, inserisce nome, cognome e matricola del delegante e carica la domanda compilata in PDF o JPG, massimo 3 MB. Per ciascun delegante può esserci una sola richiesta attiva fra i due percorsi.
4. La platea congelata all’apertura determina il limite per ciascun delegato. Fino a 500 aventi diritto inclusi il massimo è 3. Da 501 in poi il massimo è 5. Le richieste del delegato in verifica e le deleghe approvate da entrambi i percorsi occupano lo stesso limite. Il delegato può ritirare una propria richiesta con allegato finché è in verifica.
5. Il delegato apre la propria sessione con il suo link personale e seleziona **Apri scheda in delega**. La scheda indica il rappresentato prima della conferma. La stessa posizione accetta una sola scheda valida. Una delega può essere revocata prima che il delegato acquisisca la prima scheda per quella posizione. Fino all’approvazione il delegante può votare direttamente.

Per le richieste presentate dal delegato, lo staff trova **Scarica allegato** nella sezione **Presenze e deleghe**. Esamina il documento e verifica che corrisponda alla persona e alla consultazione prima di premere **Approva**. L’approvazione accoda un’email al delegato con un link dedicato a quella scheda. La tabella indica l’esito dell’invio. L’allegato è riservato allo staff e resta fuori da email e rapporti.

In sviluppo, controlla la notifica in Mailpit. Il link per una delega apre una sessione per la singola scheda rappresentata. Per tornare alla tua scheda e alla gestione delle deleghe, riapri l’invito personale originale. Una revoca o la rigenerazione del link personale del delegato rende inutilizzabile il link specifico già inviato.

## Inviare un sollecito ai non votanti

1. Con la consultazione aperta, seleziona **Invia sollecito ai non votanti**. Mantieni **Tutte le sezioni** oppure restringi a una sezione della platea. Sono eleggibili i soci senza scheda acquisita. Una scheda bianca, un’astensione confermata e una scheda acquisita che il socio sta sostituendo valgono già come voto.
2. Modifica **Oggetto** e **Messaggio** se necessario. Il sistema aggiunge nome del socio, titolo, scadenza Europe/Rome, link personale attivo, modalità palese e recapito di assistenza. Usa il modello neutro del sistema o un testo equivalente, privo di link e credenziali.
3. Seleziona **Verifica destinatari e messaggio**. Confronta posizioni verificate, email da accodare, esclusioni e motivazioni. In tabella trovi l’ultimo sollecito accettato da SMTP e il relativo conteggio per socio. L’esempio personalizzato mostra un segnaposto per il link.
4. Premi **Invia sollecito a N soci**. La conferma vale per i destinatari idonei mostrati nell’anteprima. Se vuoi cambiare sezione o testo, prepara una nuova anteprima. Le anteprime scadono dopo 30 minuti. Il numero di messaggi può diminuire alla conferma o durante l’elaborazione, per esempio se nel frattempo un socio vota o scade la consultazione.
5. Segui la campagna da **Storico invii** e premi **Aggiorna esiti** per aggiornare i conteggi. **Annulla gli invii ancora in coda** annulla le righe pendenti. Per i messaggi già presi in carico, consulta l’esito del tentativo. Il server di posta conserva i messaggi già affidati.

Le esclusioni riportano il motivo, fra cui voto già acquisito, chiusura o scadenza, credenziale assente o non valida, email mancante o inutilizzabile, invio pendente o intervallo minimo ancora in corso. Il worker verifica di nuovo l’idoneità a ogni tentativo. Le email sono individuali anche in presenza di un indirizzo condiviso da più soci.

L’intervallo iniziale è di 24 ore fra solleciti alla stessa posizione elettorale e tiene conto anche degli esiti incerti. Un sollecito già pendente impedisce di accodarne un altro per lo stesso socio. Per cambiare l’intervallo, apri **Storico invii**, imposta un valore da 1 a 720 ore, indica la motivazione e usa **Salva intervallo e motivazione**. L’operazione resta nel registro applicativo. Il primo invito e il reinvio individuale hanno controlli distinti dal limite dei solleciti.

### Leggere gli esiti e gestire i recapiti

| Esito | Lettura operativa |
| --- | --- |
| In coda | Il worker deve ancora preparare il tentativo o attende un nuovo tentativo dopo un errore temporaneo. |
| In invio | Il worker ha preso in carico il messaggio. Il socio può votare mentre l’email è in transito. |
| Accettato da SMTP | Mailpit ha accettato il messaggio. Per la prova, apri la casella locale e verifica destinatario, contenuto e link. |
| Fallito | L’errore è definitivo o sono terminati i tentativi ammessi. Esamina il motivo e verifica il recapito della posizione. |
| Esito incerto | Il server potrebbe avere già accettato il messaggio. Controlla Mailpit cercando ora, oggetto e destinatario prima di decidere se reinviare. |
| Escluso | Il motivo è nella riga. Per email o credenziale, verifica la posizione e correggi il problema con l’azione dedicata. |
| Annullato | L’operatore ha annullato il messaggio ancora in coda. |

Se correggi un recapito, controlla l’esito già registrato. Per ripetere un invio escluso, prepara un reinvio individuale o una nuova campagna quando il socio è ancora idoneo. Se sospetti un invio della credenziale alla persona sbagliata, revoca il link prima di consegnare quello nuovo. Per la dimostrazione usa esclusivamente indirizzi `@example.invalid`.

## Chiudere e scrutinare

La dashboard mostra aventi diritto, votanti unici, affluenza, scadenza e indicatori SMTP durante l’apertura. Alla scadenza il server rifiuta nuove schede e sostituzioni, anche se il worker deve ancora aggiornare lo stato. Puoi anticipare la fine con **Chiudi la consultazione** e confermare. Usa **Annulla consultazione** con una motivazione quando occorre fermare o invalidare la consultazione. La cancellazione è riservata alla bozza.

Dallo stato **Chiusa**, seleziona **Pubblica lo scrutinio** per rendere disponibili i risultati all’amministrazione. **Risultati e report** contiene lo scrutinio per quesito, il CSV dei risultati, il CSV separato di partecipazione e il PDF riepilogativo non firmato. Per ogni quesito verifica le schede con risposta, le schede che lo hanno lasciato vuoto e le preferenze per opzione. Nei quesiti multipli le preferenze complessive possono superare il numero di schede con risposta.

Il CSV di partecipazione contiene nominativi, sezione, stato di voto e orario dell’ultima acquisizione. Custodiscilo come documento riservato. Per la verbalizzazione distingui aventi diritto, votanti e presenze assembleari e applica le regole operative approvate dall’associazione.

## Quorum e rapporto assembleare

1. Apri **Quorum assembleare** dalla bozza o da una consultazione chiusa. Dopo la pubblicazione dello scrutinio lo trovi in **Risultati e report → Rapporto assembleare**.
2. Seleziona **Presenze confermate** oppure **Presenze confermate e deleganti con scheda acquisita**. Nel secondo caso un socio presente e anche rappresentato conta una sola volta. Una delega approvata conta come esercitata quando esiste una scheda acquisita per quella posizione, anche bianca.
3. Registra la soglia come numero di persone oppure come frazione degli aventi diritto congelati all’apertura. Per una soglia strettamente superiore alla metà, scegli **Più di**, numeratore `1` e denominatore `2`. Con otto aventi diritto il minimo risultante è cinque. Puoi indicare il riferimento statutario. Il rapporto riporta la data dell’ultimo salvataggio e distingue il raggiungimento matematico di una soglia dal caso in cui il criterio sia ancora da configurare.
4. Dopo lo scrutinio, usa **Scarica rapporto PDF** per il documento o **Scarica pacchetto con impronte** per ottenere lo stesso tipo di rapporto insieme ai prospetti di risultati, partecipazione, presenze, deleghe e scrutinio generati in un’unica esportazione.

Il PDF mostra identificativo, giorni e orari effettivi di apertura e chiusura, ora di esportazione in Europe/Rome, esiti per quesito e impronte SHA-256 dei prospetti. I partecipanti unici nel riepilogo sono le persone che hanno confermato presenza o inviato una scheda, di persona o per delega. Il calcolo del quorum usa il criterio scelto al punto 2. In `deleghe.csv` trovi per ogni richiesta stato e presenza di una scheda acquisita per delega.

Il pacchetto contiene i dati di presenze e deleghe e va custodito come materiale riservato. Dopo l’estrazione, esegui `shasum -a 256 -c SHA256SUMS.txt` dalla cartella dei file per confrontare gli SHA-256, compreso quello del PDF contenuto nello stesso pacchetto.

Il rapporto verifica matematicamente la soglia registrata. La qualificazione giuridica delle partecipazioni elettroniche e la valida costituzione dell’assemblea restano da accertare secondo il procedimento concordato con il notaio.
