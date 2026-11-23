#!/bin/sh
set -eu

psql --username postgres --dbname postgres --set=ON_ERROR_STOP=1 <<'SQL'
\getenv app_user AIM_DB_USER
\getenv app_password AIM_DB_PASSWORD
\getenv app_database POSTGRES_DB
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'app_database', :'app_user') \gexec
SQL
