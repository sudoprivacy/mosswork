#!/bin/sh
set -eu
: "${MOSSWORK_APP_PASSWORD:?required}" "${IM_APP_PASSWORD:?required}"
[ "$MOSSWORK_APP_PASSWORD" != "$IM_APP_PASSWORD" ] || { echo 'Use distinct application passwords' >&2; exit 1; }
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
 --set mosswork_pw="$MOSSWORK_APP_PASSWORD" --set im_pw="$IM_APP_PASSWORD" <<'SQL'
CREATE ROLE mosswork_app LOGIN PASSWORD :'mosswork_pw';
CREATE ROLE im_app LOGIN PASSWORD :'im_pw';
CREATE DATABASE mosswork OWNER mosswork_app;
CREATE DATABASE mosswork_im OWNER im_app;
REVOKE ALL ON DATABASE mosswork FROM PUBLIC;
REVOKE ALL ON DATABASE mosswork_im FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
GRANT CONNECT ON DATABASE mosswork TO mosswork_app;
GRANT CONNECT ON DATABASE mosswork_im TO im_app;
\connect mosswork
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO mosswork_app;
\connect mosswork_im
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO im_app;
SQL
