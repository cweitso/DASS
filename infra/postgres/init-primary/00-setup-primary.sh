#!/bin/bash
# Fail fast.
set -e

echo ">>> Configuring replication on the primary..."

# 1. Replication role.
# Wrapped in a DO block so re-running against an existing volume is idempotent
# instead of failing on "role already exists".
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN
        CREATE ROLE replicator WITH REPLICATION PASSWORD 'replicator_password' LOGIN;
      END IF;
    END
    \$\$;
EOSQL
echo ">>> Replication role ready."

# 2. Allow the replicator role to open replication connections from any host.
# The grep guard keeps a retained volume from collecting duplicate rules.
if ! grep -q "^host replication replicator" "$PGDATA/pg_hba.conf"; then
    echo "host replication replicator all md5" >> "$PGDATA/pg_hba.conf"
    echo ">>> pg_hba.conf rule added."
else
    echo ">>> pg_hba.conf already allows replicator, skipping."
fi

# 3. WAL settings. PostgreSQL 16 already defaults to wal_level=replica; setting it
# explicitly documents the intent. Replication slots let a disconnected replica
# resume without the primary having recycled the WAL it still needs.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    ALTER SYSTEM SET wal_level = replica;
    ALTER SYSTEM SET max_wal_senders = 10;
    ALTER SYSTEM SET max_replication_slots = 10;
EOSQL
echo ">>> WAL settings applied."
