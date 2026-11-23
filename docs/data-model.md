# Modello dati

| Entità | Dati principali e vincoli |
| --- | --- |
| `Member` | ID interno, ID esterno stabile e univoco, nome, cognome, email, cellulare opzionale e sezione. Le email possono essere condivise. |
| `ImportBatch` | Righe caricate temporaneamente, mappatura, autore, scadenza e riepilogo. La conferma svuota i dati di anteprima. |
| `Election` | Titolo, descrizione, calendario, modalità palese vincolata, stato, regola di sostituzione e intervallo dei solleciti. |
| `Question`, `Choice` | Quesiti e relative opzioni. Il tipo determina il massimo di preferenze. |
| `ElectionDocument` | PDF privato associato alla consultazione, servito come allegato dopo verifica dell'autorizzazione. |
| `ElectionVoter` | Una posizione per coppia socio-consultazione. Fotografa ID esterno, generalità, sezione e recapiti. Le correzioni dei recapiti hanno un flusso dedicato. |
| `VotingCredential` | Una credenziale attiva per posizione. Il digest SHA-256 è univoco e indicizzato. Il valore cifrato Fernet consente il recupero autorizzato. La versione invalida le sessioni precedenti dopo una revoca. |
| `VoterPresence` | Una conferma con data e ora per posizione elettorale. |
| `Delegation` | Delegante, delegato, data e stato della richiesta. Vincoli per persone distinte e una sola richiesta attiva per delegante. Servizi e lock verificano la platea comune e la capienza del delegato. |
| `NamedBallot` | Una scheda corrente per `ElectionVoter`, con risposte, revisione e istante di acquisizione. La revisione aumenta a ogni sostituzione valida. Per una scheda per delega, `cast_by` registra la posizione del delegato. |
| `SubmissionReceipt` | ID della richiesta, impronta HMAC, revisione e istante. Vincoli univoci per posizione/richiesta e posizione/revisione. Le ricevute delle revisioni precedenti non conservano le scelte in chiaro. |
| `ReminderCampaign` | Campagna per invito, sollecito o reinvio, con autore, consultazione, modello, avanzamento e date di conferma e annullamento. |
| `EmailDelivery` | Posizione nella selezione di una campagna, stato, tentativi, motivo ed esito. Vincoli per coppia campagna-elettore e per sollecito pendente sulla stessa posizione. |
| `AuditEvent` | Metadati operativi, attori, date e riferimenti. Le operazioni sui link registrano posizione e azione, senza il valore della credenziale. |
| `RateBucket` | Contatori persistenti con chiave derivata per limitare le richieste di autenticazione e riscatto. |

I risultati aggregati si calcolano dalle schede correnti dopo la pubblicazione dello scrutinio. Una scheda senza preferenze è bianca. Un singolo quesito omesso è una risposta mancante, anche su una scheda altrimenti compilata. “Astenuto” è un'opzione espressa. Il numero dei votanti è quello delle posizioni con scheda acquisita. L'accesso al link e gli eventi email non attestano presenza in assemblea.
