# Backup e ripristino

La copia PostgreSQL contiene soci, schede nominative, schede segrete cifrate, registri e credenziali cifrate. Conserva anche il valore di `DJANGO_SECRET_KEY` in un archivio separato e riservato per poter rileggere le schede segrete dopo un ripristino. Proteggila con cifratura, custodia e cancellazione controllata. Conserva separatamente il materiale necessario a leggere ciascuna copia, compreso l'insieme delle chiavi Fernet in uso nel relativo periodo. Dopo un ripristino possono riapparire vecchi stati delle campagne, dei link e delle sessioni. Tieni sospesi web e worker fino alla riconciliazione.

## Accesso tecnico locale

```sh
docker compose exec db sh -c 'exec psql -U postgres -d "$POSTGRES_DB"'
```

Il comando richiede accesso a Docker sull'host. L'account tecnico PostgreSQL `postgres` ha credenziali distinte dal ruolo applicativo `aim_voto`. Il ruolo applicativo possiede il proprio database ed è senza privilegi di superutente. PostgreSQL non pubblica porte su host. Per un server remoto usa un canale autenticato e cifrato riservato ai tecnici autorizzati.

## Prova su dati sintetici

Dopo avere completato lo scenario demo, crea una copia con permessi restrittivi. Il seguente esempio presuppone il ruolo di sviluppo predefinito `aim_voto` e crea un database separato `aim_voto_restore`. Se un ambiente ha nomi diversi, sostituiscili esplicitamente. Usa un nome di destinazione nuovo a ogni ripristino.

```sh
umask 077
mkdir -p backups
docker compose exec -T db sh -c 'exec pg_dump -U postgres -d "$POSTGRES_DB" -Fc' > backups/demo.dump
test -s backups/demo.dump
docker compose exec -T db createdb -U postgres -O aim_voto aim_voto_restore
docker compose exec -T db pg_restore -U postgres -d aim_voto_restore \
  --exit-on-error --no-owner --no-privileges --role=aim_voto < backups/demo.dump
docker compose exec -T db psql -U postgres -d aim_voto_restore -c \
  'SELECT count(*) AS schede_correnti FROM voting_namedballot'
```

Il database ripristinato deve contenere le due schede dello scenario. Confronta anche soci, elettori, ricevute e stati delle consultazioni con l'originale. Per una verifica applicativa usa un progetto Compose isolato, senza worker e destinatari reali, con i segreti dell'archivio. Controlla integrità, accesso autorizzato, riepilogo e scadenze prima di qualsiasi riapertura del servizio.

## Procedura in caso di incidente

1. Sospendi le operazioni e la posta, identifica un'istantanea coerente e conserva le evidenze con accesso ristretto.
2. Ripristina in isolamento con versione e migrazioni compatibili. Verifica che le chiavi necessarie siano disponibili e che quella corrente appartenga all'ambiente previsto.
3. Confronta schede e ricevute dopo il punto di ripristino, eventuali revoche e messaggi già affidati a SMTP. Riconcilia esiti incerti e campagna prima di riprendere gli invii.
4. Concorda con i responsabili dell'associazione il trattamento degli invii successivi all'istantanea. Documenta eventuale perdita di dati e la procedura adottata prima di riaprire l'accesso.
5. Preserva i backup tecnici secondo le regole di conservazione e verifica periodicamente il ripristino. Per dati reali definisci obiettivi di recupero e perdita massima, con allarmi e collaudi periodici.
