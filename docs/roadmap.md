# Roadmap

## Prima di un pilota

1. Formalizzare le regole della consultazione, i ruoli e la procedura per verificare l'identità prima di correggere recapiti, condividere un link o approvare una delega. Validare il flusso di conferma presenze, revoca e voto per delega sulle prassi associative. Definire quorum e maggioranze prima di un eventuale sviluppo applicativo.
2. Preparare un ambiente separato con HTTPS, protezione del proxy, account individuali con MFA, monitoraggio, gestione dei segreti con accessi controllati e audit inoltrato a un archivio indipendente. Definire come raccogliere e conservare evidenze in caso di incidente.
3. Provare backup e ripristino di database e chiavi, includendo un confronto di schede, ricevute e coda dopo il ripristino. Stabilire chi decide la gestione delle acquisizioni successive all'ultimo backup, delle campagne in corso e degli esiti SMTP incerti.
4. Aggiungere un profilo per SMTP reale con autenticazione, TLS, dominio verificato, gestione dei rimbalzi e test senza recapiti dei soci. Il rilascio attuale mantiene gli invii nel circuito dimostrativo.
5. Misurare l'apertura, gli accessi e le conferme concorrenti fino a 2.000 posizioni. Provare la perdita temporanea del worker e il comportamento a ridosso della chiusura. Validare accessibilità e compatibilità su telefoni fisici e client di posta rappresentativi.

## Rilasci successivi

- Integrare un motore di voto segreto verificabile dopo una prova con Helios, fiduciari ed elettori fittizi. Verificare con revisione indipendente come esercitare le deleghe senza ricostruire il collegamento tra identità e preferenze.
- Introdurre ruoli limitati per osservatori e, se necessari, amministratori di sezione.
- Collegare l'anagrafica esterna con riconciliazione per ID stabile e approvazione della platea.
- Aggiungere le regole assembleari effettivamente approvate e la conservazione indipendente degli eventi.
