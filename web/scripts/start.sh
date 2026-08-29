#!/bin/sh
set -e
echo "Migrazioni..."
node db/migrate.js
echo "Avvio..."
exec node src/index.js
