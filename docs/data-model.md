# Modello dati

| Entità | Dati principali e vincoli |
| --- | --- |
| `Member` | Anagrafica preesistente, conservata per i riferimenti storici. |
| `ImportBatch` | Consultazione, righe caricate temporaneamente, mappatura, autore, scadenza e riepilogo. La conferma svuota i dati di anteprima. |
| `Election` | Titolo, descrizione, calendario, modalità palese o segreta, stato, opzione di sostituzione e intervallo dei solleciti. Per le assemblee, tipologia, convocazione, totale degli associati e configurazione delle soglie congelata all’apertura. Conserva anche i dati facoltativi del verbale, ossia nominativi dei firmatari, città e data di firma, ordine del giorno e discussione. |
| `ElectionAdministrator` | Profilo univoco per account staff con accesso limitato alle consultazioni assegnate. Il profilo resta limitato anche quando l’elenco è vuoto dopo revoche o eliminazioni. |
| `AssemblyQuorum` | Impostazione facoltativa per consultazione. Registra criterio di conteggio, confronto, soglia assoluta o frazionaria, eventuale riferimento statutario, autore e data dell’ultimo salvataggio. |
| `AssemblyVerification` | Verifica assembleare con revisione univoca per consultazione. Registra Presidente, riferimento all’attestazione, autore, data, base di partecipazione, risultati aggregati ed eventuale motivazione della rettifica. La rettifica aggiunge una revisione. |
| `Question`, `Choice` | Quesiti e opzioni. Il tipo determina il massimo di preferenze. Il minimo di votanti e il criterio deliberativo si configurano in campi indipendenti. |
| `ElectionDocument` | PDF privato associato alla consultazione, servito come allegato dopo verifica dell'autorizzazione. |
| `ElectionVoter` | Una posizione autonoma per coppia consultazione-matricola. Conserva generalità, sezione e recapiti usati nella votazione. Le posizioni preesistenti mantengono il riferimento facoltativo a `Member`. |
| `VotingCredential` | Una credenziale attiva per posizione. Il digest SHA-256 è univoco e indicizzato. Il valore cifrato Fernet consente il recupero autorizzato. La versione invalida le sessioni precedenti dopo una revoca. |
| `VoterPresence` | Una presenza con data, ora e origine per posizione elettorale. L’origine indica la conferma esplicita o la prima scheda acquisita per quella persona come votante. |
| `Delegation` | Delegante, delegato, data e stato della richiesta. Vincoli per persone distinte e una sola richiesta attiva per delegante. Servizi e lock verificano la platea comune e la capienza del delegato. |
| `DelegationAttachment` | PDF o JPG privato della richiesta presentata dal delegato, con nome del file e tipo verificato. Il documento è consultabile dallo staff della consultazione. |
| `NamedBallot` | Una scheda corrente per `ElectionVoter`, con risposte, revisione e istante di acquisizione. La revisione aumenta a ogni sostituzione valida. Per una scheda per delega, `cast_by` registra la posizione del delegato. |
| `SecretBallot` | Scheda corrente cifrata di lunghezza uniforme, con consultazione e UUID. L’UUID è casuale per il voto definitivo e derivato con chiave applicativa per ogni revisione se la sostituzione è consentita. |
| `SubmissionReceipt` | ID della richiesta, impronta HMAC, revisione e istante. Vincoli univoci per posizione/richiesta e posizione/revisione. Nel segreto funge anche da registro di partecipazione e conserva l’eventuale delegato. L’impronta del segreto dipende solo dai dati di acquisizione. |
| `ReminderCampaign` | Campagna per invito, sollecito, reinvio, link di delega o risultati. Un vincolo consente una sola campagna di risultati per consultazione. |
| `EmailDelivery` | Posizione nella selezione di una campagna, stato, tentativi, motivo ed esito. Vincoli per coppia campagna-elettore, per invio ordinario pendente e per una sola notifica di approvazione per delega. |
| `AuditEvent` | Metadati operativi, attori, date e riferimenti. Le operazioni sui link registrano posizione e azione, senza il valore della credenziale. |
| `RateBucket` | Contatori persistenti con chiave derivata per limitare le richieste di autenticazione e riscatto. |

I risultati aggregati si calcolano dalle schede correnti dopo la pubblicazione dello scrutinio. Una scheda senza preferenze è bianca. Un singolo quesito omesso è una risposta mancante, anche su una scheda altrimenti compilata. “Astenuto” è un'opzione espressa. Il numero dei votanti è quello delle posizioni con scheda acquisita. L'accesso al link e gli eventi email non attestano presenza in assemblea.
