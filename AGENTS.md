# Istruzioni per AIM Voto

Lavora sempre in modo conservativo e incrementale.

## Prima di modificare il codice

- Leggi la struttura del repository.
- Identifica i file realmente coinvolti.
- Spiega brevemente il piano di intervento.
- Evita refactoring massivi non richiesti.

## Regole operative

- Crea sempre modifiche minime e coerenti con lo stile esistente.
- Non cambiare stack tecnologico, framework, struttura directory o naming senza esplicita richiesta.
- Non modificare file generati se non è necessario o se non hai capito da quale comando vengono rigenerati.
- Non committare mai file `.env`, credenziali, token, screenshot, asset temporanei, log o file locali. Verifica file tracciati e cronologia prima del push.
- Non usare `git push --force`. Se ci sono conflitti Git, fermati e spiegali prima di risolverli.
- Se devi modificare schema database, API OpenAPI o tipi generati, spiega chiaramente l’impatto.
- Preferisci un branch per task e, quando possibile, una Pull Request piccola e revisionabile.
- Usa soltanto anagrafiche sintetiche e indirizzi `example.invalid` in fixture, esempi e test.

## Validazione

- Dopo le modifiche esegui `docker compose --profile test run --build --rm test`. La verifica include Ruff, i controlli Django, il confronto delle migrazioni e la suite PostgreSQL.
- Per rigenerare le migrazioni usa `docker compose run --rm --no-deps -v "$PWD:/app" -u "$(id -u):$(id -g)" web python manage.py makemigrations` con PostgreSQL già avviato.
- Se disponibili nel progetto, esegui anche `pnpm install`, `pnpm run typecheck` e `pnpm --filter @workspace/api-server run test`. La base Django non usa pnpm.
- Se mancano ambiente, dipendenze o database, indica quali test non sono stati eseguiti e riporta il comando esatto da eseguire localmente. Dichiara l’esito solo dei test eseguiti.

## Risposte

Rispondi in italiano con linguaggio tecnico e chiaro. Riporta sempre i file modificati, i rischi, le assunzioni e gli eventuali passaggi manuali.
