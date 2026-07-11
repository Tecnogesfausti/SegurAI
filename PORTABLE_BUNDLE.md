# SegurAI Portable Bundle

Este paquete contiene lo necesario para continuar SegurAI en otro equipo:

- codigo principal: `segurai.py`
- agentes: `agents/`
- reglas de modelos: `model_routes.yaml`
- dependencias: `requirements.txt`
- instalador local: `install_segurai.sh`
- memoria/estado SQLite: `segurai_memory.sqlite3`
- variables y claves: `.env`
- documentacion: `README.md`

## Instalacion En Otro Equipo

```bash
tar -xzf segurai-portable-YYYYMMDD-HHMMSS.tar.gz
cd SegurAI
./install_segurai.sh
```

## Arranque

```bash
source .venv/bin/activate
python3 segurai.py --no-sensor-loop
```

Luego prueba:

```text
/salud
/router
/agentes
/coste
```

## Seguridad

El archivo `.env` incluye claves/tokens. Trata este paquete como secreto.
