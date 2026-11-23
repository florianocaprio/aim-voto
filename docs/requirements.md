# Requisiti di AIM Voto

AIM Voto supporta consultazioni online di Angeli in Moto ODV. L’amministratore nazionale può comporre una platea per l’intera associazione, per sezione o per soci selezionati. L’elettore accede dal telefono o dal computer con il link personale ricevuto al recapito associativo. L’interfaccia operativa è in italiano.

Il primo rilascio implementa il **voto palese e nominativo**. La scheda è collegata all’elettore e l’interfaccia lo comunica prima della conferma. L’opzione **Segreto** compare disabilitata nel menu **Modalità di voto** e il server rifiuta la creazione o l’apertura di una consultazione segreta. Il primo rilascio operativo del voto segreto dovrà supportare sia il voto personale sia il voto per delega, con un motore verificabile.

## Primo rilascio

| Area | Requisito operativo |
| --- | --- |
| Platea | Inserimento manuale o importazione CSV e XLSX in bozza, con associazione delle colonne, validazione per riga e conferma. Nome, cognome, matricola univoca nella consultazione ed email sono obbligatori. Cellulare e sezione sono facoltativi. |
| Consultazione | Titolo, descrizione, documenti PDF privati, platea da 1 a 2.000 soci, apertura manuale o programmata, scadenza anche su più giorni, voto sostituibile per impostazione iniziale. Date presentate in Europe/Rome e archiviate come istanti UTC. |
| Quesiti | Mozione con Favorevole, Contrario e Astenuto, scelta singola, scelta multipla e selezione di candidati. Il server valida appartenenza e numero delle preferenze. |
| Accesso degli elettori | Credenziale personale riutilizzabile durante l’apertura, con 256 bit di casualità. Il browser la scambia con una sessione circoscritta alla consultazione, al socio e alla versione della credenziale. |
| Assistenza | Ricerca per nome, ID, email o cellulare. Copia del link corrente per un invio manuale, reinvio email individuale, correzione motivata dei recapiti e revoca con rigenerazione. |
| Comunicazioni | Inviti avviati dopo l’apertura, solleciti collettivi ai non votanti con anteprima e conferma, reinvio individuale. Un messaggio per posizione elettorale, coda persistente, controllo della frequenza ed esiti registrati. |
| Partecipazione | Conteggi degli aventi diritto e dei votanti unici, affluenza, scadenza e indicatori SMTP. Ogni scheda acquisita, anche bianca, vale come partecipazione alla consultazione. |
| Presenze | Conferma esplicita del socio durante l’apertura. Elenco e CSV amministrativi separati dal voto. |
| Deleghe | Presentazione online dal socio, verifica dello staff, revoca fino alla prima scheda per delega e blocco del doppio esercizio. Ciascun delegato può esercitare al massimo tre deleghe fino a 500 aventi diritto inclusi e cinque da 501. |
| Scrutinio | Risultati aggregati dopo chiusura e pubblicazione amministrativa dello scrutinio. CSV dei risultati, CSV separato di partecipazione e riepilogo PDF non firmato a supporto del verbale. |
| Esercizio | Ambiente locale avviabile con Docker Compose, PostgreSQL, worker persistente e Mailpit. Accesso amministrativo con autenticazione Django e MFA TOTP configurabile. Migrazioni versionate e CI con controlli statici, test e scansione dei segreti. |

## Regole applicative

- La matricola identifica la posizione nella stessa consultazione anche se cambia email. L’importazione segnala gli indirizzi condivisi e mantiene separate le posizioni. Le matricole duplicate nel file bloccano la conferma. Dopo la correzione degli errori e la conferma, il sistema applica tutte le righe in una sola transazione.
- All’apertura restano fissati aventi diritto, quesiti, opzioni e regole. Per correggere un recapito si usa l’azione dedicata sulla posizione elettorale. Una consultazione conclusa conserva i dati degli elettori usati per quella votazione.
- L’apertura del link consente di consultare la scheda. Per registrarla occorrono riepilogo e conferma esplicita. L’accesso, l’acquisizione della scheda e l’eventuale presenza assembleare sono eventi distinti.
- Per ogni socio si conta l’ultima scheda validamente registrata prima della chiusura. Una nuova scheda sostituisce per intero la precedente al termine della transazione. Una richiesta ripetuta può recuperare la stessa ricevuta e una conferma con revisione superata richiede una nuova compilazione.
- Una scheda bianca è un invio valido con tutti i quesiti vuoti. Una risposta mancante è un quesito lasciato vuoto in una scheda acquisita. **Astenuto** è una preferenza esplicita del quesito a mozione. Il mancato invio resta registrato come mancata partecipazione al voto.
- L’anteprima dei solleciti parte dagli aventi diritto della sezione scelta o dell’intera platea. Accoda i soci idonei senza scheda acquisita. Il controllo si ripete alla conferma e alla preparazione di ciascun tentativo di invio.
- La coda distingue accettazione SMTP, errori temporanei o permanenti, esiti incerti ed esclusioni. L’accettazione indica la presa in carico da parte del server SMTP. Mailpit riceve soltanto invii di prova a recapiti `@example.invalid`.
- In questa versione l’amministratore nazionale autorizzato opera su tutte le consultazioni. Il recupero di un link consente tecnicamente di accedere come il socio. I privilegi del database consentono di leggere o alterare schede e registro applicativo.

## Stati della consultazione

| Stato | Operazioni |
| --- | --- |
| Bozza | Modifica di regole, quesiti, documenti e platea. Eliminazione della bozza. Apertura immediata o programmazione. |
| Programmata | Apertura automatica all’ora impostata con worker attivo. Sono disponibili apertura immediata, ritorno in bozza e annullamento motivato. |
| Aperta | Voto, eventuale sostituzione, comunicazioni e monitoraggio della partecipazione. Chiusura manuale, automatica a scadenza o annullamento motivato. |
| Chiusa | Pubblicazione amministrativa dello scrutinio o annullamento motivato. Le schede correnti restano conservate. |
| Scrutinata | Risultati aggregati, esportazioni e report. |
| Annullata | Conservazione della consultazione e delle schede acquisite. |

## Criteri di verifica

Lo scenario dimostrativo importa tre soci sintetici, apre una consultazione, verifica gli inviti in Mailpit e registra due schede. Dopo una sostituzione, il sollecito deve partire soltanto per il terzo socio con il suo link corrente. I risultati devono mostrare due votanti e conteggiare l’ultima scheda del socio che ha votato di nuovo.

Le verifiche di concorrenza e scadenza si svolgono su PostgreSQL. La platea operativa attesa è nell’ordine di 700–1.500 soci, con un limite applicativo di 2.000 elettori per consultazione. Una prova di carico con tempi e tassi di errore misurati è un’attività distinta dalla verifica di quel limite.

Per il pilota restano da completare configurazione operativa, prova di recupero, verifica dei recapiti, revisione delle regole assembleari e misure organizzative. Gli osservatori, l’anagrafica esterna e un motore di voto segreto verificabile appartengono alla roadmap. [Modello di fiducia](security-and-trust-model.md), [backup e ripristino](backup-restore.md), [guida amministrativa](admin-guide.md).
