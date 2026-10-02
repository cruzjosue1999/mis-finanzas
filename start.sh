#!/usr/bin/env bash
# Arranca Mis Finanzas en local.
# Uso: ./start.sh   (puerto por defecto 8090; cámbialo con PORT=9000 ./start.sh)
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8090}"

if [ ! -d venv ]; then
  echo "Creando entorno virtual e instalando dependencias…"
  python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt
fi

export PORT
echo "Iniciando en el puerto $PORT…"
exec ./venv/bin/python -c "
import os
os.environ.setdefault('PORT', '$PORT')
import app
app.init_db()
print(f'Listo: http://localhost:{os.environ[\"PORT\"]}')
from waitress import serve
serve(app.app, host='0.0.0.0', port=int(os.environ['PORT']))
"
