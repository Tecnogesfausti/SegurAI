# SegurAI

Agente de terminal 24/7 con:

- conexión MCP HTTP hacia Home Assistant
- conexión MCP por `stdio` como alternativa
- OpenRouter compatible con DeepSeek
- memoria local en SQLite
- observación periódica de sensores
- comandos interactivos para consultar memoria, sensores y herramientas

## Instalación

```bash
cd /home/lego/sensores/SegurAI
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Configuración

```bash
export OPENROUTER_API_KEY="sk-or-..."
export SEGURAI_MODEL_ROUTES="model_routes.yaml"
export SEGURAI_AGENTS_DIR="agents"
export SEGURAI_LOG_FILE="segurai_runtime.log"
export HA_MCP_URL="http://supervisor/core/api/mcp"
export HA_TOKEN="token-de-larga-duracion-de-home-assistant"
export SEGURAI_POLL_SECONDS="300"
export SEGURAI_SENSOR_PROMPT="Consulta sensores de presencia, puertas, ventanas, movimiento, alarma y cámaras si existen."
export SEGURAI_FS_ROOTS="/home/lego/sensores/SegurAI"
```

También puedes poner esas variables en `.env`. Ese archivo está ignorado por git.

## Arranque con Home Assistant MCP HTTP

Si tienes `HA_MCP_URL` y `HA_TOKEN` en `.env`:

```bash
python3 segurai.py
```

O explícitamente:

```bash
python3 segurai.py --mcp-url http://supervisor/core/api/mcp
```

## Arranque con MCP por stdio

Si algún día usas un servidor MCP externo por comando, pásalo después de `--`.

Ejemplo de forma:

```bash
python3 segurai.py -- npx -y <servidor-mcp-home-assistant>
```

## Uso

Dentro del terminal:

```text
/help
/memoria
/sensores
/agentes
/agente iniciar monitor_temperatura
/agente run monitor_temperatura
/tareas
/cancelar 3
/editar 3 | 2026-07-10T23:00:00+02:00 | Encender luz | Enciende la luz del salon
/herramientas
/router
/coste
/logs
/salud
/estado
/aprender Mi horario normal entre semana es salir de casa a las 8:15.
/salir
```

## Ficheros Del Sistema

SegurAI puede listar, leer, crear y borrar ficheros dentro de las rutas permitidas por `SEGURAI_FS_ROOTS`.

Además el agente tiene herramientas internas para:

- leer sensores desde ficheros JSON, YAML, CSV o `clave=valor` con `sensor_read_file`
- contar líneas, frases y coincidencias exactas en ficheros con `fs_count_text`
- guardar/exportar datos en JSON, JSONL o texto con `data_write_file`
- consultar URLs públicas `http/https` y extraer texto básico con `web_fetch_url`

Las herramientas de ficheros siguen limitadas por `SEGURAI_FS_ROOTS`. La herramienta web bloquea destinos locales/privados básicos y no reenvía cabeceras sensibles como `Authorization` o `Cookie`.

Comandos directos:

```text
/ls .
/leer README.md
/escribir notas/prueba.txt | contenido de prueba
/borrar notas/prueba.txt
```

Para permitir más rutas, sepáralas por coma:

```bash
export SEGURAI_FS_ROOTS="/home/lego/sensores/SegurAI,/home/lego/documentos"
```

Dar acceso a todo el sistema con `/` es posible si el usuario del proceso tiene permisos, pero es peligroso:

```bash
export SEGURAI_FS_ROOTS="/"
```

## Herramientas Home Assistant Extra

Además de las herramientas MCP de Home Assistant, SegurAI añade herramientas REST internas cuando hay `HA_MCP_URL` y `HA_TOKEN`:

- `ha_get_states`: listar estados actuales con filtros por dominio/texto
- `ha_get_state`: leer una entidad concreta
- `ha_search_entities`: buscar entidades por texto
- `ha_get_services`: listar servicios disponibles
- `ha_call_service`: ejecutar servicios genéricos con confirmación
- `ha_render_template`: renderizar plantillas Jinja de Home Assistant
- `ha_get_events`: listar tipos de eventos
- `ha_get_error_log`: leer el final del log de errores
- `ha_get_history`: consultar histórico
- `ha_get_logbook`: consultar logbook

`ha_call_service` requiere `confirm=true` cuando la configuración exige confirmación para acciones.

## Coste Y Tokens

SegurAI guarda el uso de tokens y el coste estimado de cada llamada al modelo en SQLite.

```text
/coste
/estado
```

El coste es estimado usando la tabla pública de precios de OpenRouter al arrancar. Si OpenRouter no devuelve tokens para una llamada, esa llamada queda registrada sin coste estimado.

## Seleccion Inteligente De Modelos

Todas las llamadas al LLM pasan por `ModelRouter`. Las reglas viven en `model_routes.yaml`, no en el codigo.

```yaml
default: openai/gpt-4.1-nano

routes:
  homeassistant:
    model: openai/gpt-4.1-nano
    requires_tools: true

  summary:
    model: google/gemini-2.5-flash-lite
```

Puedes ver la configuracion cargada con:

```text
/router
```

Y el log de uso con modelo elegido, motivo, tokens, duracion y coste con:

```text
/coste
```

## Agentes Especializados - Fase 1

Los agentes viven en `agents/` y heredan de `agents.base.Agent`.

Comandos:

```text
/agentes
/agente descubrir
/agente iniciar <nombre>
/agente detener <nombre>
/agente reiniciar <nombre>
/agente run <nombre>
```

Fase 1 incluye:

- clase base `Agent`
- metadatos obligatorios del agente
- `AgentManager`
- descubrimiento dinamico de modulos en `agents/`
- inicio/detencion/reinicio logico
- ejecucion manual `run`
- estadisticas y aislamiento de errores

La ejecucion programada automatica de agentes queda para Fase 2.

## Historial De Consola

SegurAI usa `readline` cuando está disponible para editar la línea con el cursor y recuperar comandos con flecha arriba/abajo. El historial se guarda por defecto en `.segurai_history`.

Puedes configurarlo con:

```bash
export SEGURAI_HISTORY_FILE=".segurai_history"
export SEGURAI_HISTORY_LIMIT="1000"
```

## Supervisión Y Logs

SegurAI escribe eventos en `SEGURAI_LOG_FILE` en formato JSONL. Esto permite supervisar lo que hace mientras está en marcha:

```bash
tail -f segurai_runtime.log
```

Comandos internos:

```text
/logs
/logs 100
/salud
```

`/salud` resume MCP, ModelRouter, agentes, tareas fallidas y coste acumulado.

## Funcionamiento 24/7

Mientras el proceso esté vivo, SegurAI:

1. mantiene una conversación interactiva por terminal
2. usa herramientas MCP para consultar Home Assistant cuando lo necesite
3. observa sensores cada `SEGURAI_POLL_SECONDS`
4. guarda observaciones y memorias en `segurai_memory.sqlite3`
5. ejecuta tareas programadas persistentes

Para desactivar el observador periódico:

```bash
python3 segurai.py --no-sensor-loop -- npx -y <servidor-mcp-home-assistant>
```

## Seguridad

Por defecto, el agente tiene instrucción de no cambiar estados sensibles como alarmas, sirenas, cerraduras, cámaras o automatizaciones sin confirmación explícita. Para permitir acciones directas:

```bash
python3 segurai.py --allow-actions-without-confirmation -- npx -y <servidor-mcp-home-assistant>
```

## Tareas Futuras

Puedes pedir tareas en lenguaje natural:

```text
Esta noche enciende la luz de 23:00 a 3:00 del dia siguiente.
```

El agente debe crear dos tareas:

- una para encender la luz a las 23:00
- otra para apagarla a las 03:00 del dia siguiente

Comandos útiles:

```text
/tareas
/tareas --todo
/cancelar <id>
/reintentar <id>
/editar <id> | <ISO> | <titulo> | <instruccion>
```

Las tareas simples de recordatorio se imprimen directamente sin llamar al modelo. Las tareas que requieren entender o actuar sobre Home Assistant tienen timeout configurable:

```bash
export SEGURAI_TASK_TIMEOUT_SECONDS=45
```
