#!/bin/bash
set -e

echo ">>> Replica bootstrap starting..."

# An empty PGDATA means a first start: clone the primary before serving.
if [ -z "$(ls -A $PGDATA)" ]; then
    echo ">>> Empty data directory, taking a base backup from the primary..."
    
    # Both containers start together, so wait for the primary to accept connections.
    until pg_isready -h postgres -U "$POSTGRES_USER"; do
      echo ">>> Waiting for the primary..."
      sleep 2
    done

    # -X stream: stream WAL during the backup so it is consistent on completion.
    # -R: write the connection settings the replica needs into postgresql.auto.conf.
    echo ">>> Primary is up, copying the base backup..."
    PGPASSWORD=replicator_password pg_basebackup -h postgres -D $PGDATA -U replicator -vP -X stream -R

    # standby.signal is what puts PostgreSQL into read-only replica mode.
    touch $PGDATA/standby.signal
    
    echo ">>> Base backup complete, running as a replica."
else
    # Data is already present from an earlier run.
    echo ">>> Data directory is populated, skipping the base backup."
fi

# Hand off to the official postgres entrypoint.
echo ">>> Starting PostgreSQL..."
exec docker-entrypoint.sh postgres