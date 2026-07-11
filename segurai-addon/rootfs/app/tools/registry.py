from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tools import filesystem, homeassistant, web

ToolHandler = Callable[[Any, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class BuiltinTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


BUILTIN_TOOLS: dict[str, BuiltinTool] = {
    "fs_list_dir": BuiltinTool(
        name="fs_list_dir",
        description="Lista archivos y carpetas dentro de las rutas permitidas del sistema.",
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta de directorio."},
                "limit": {"type": "integer", "default": 100},
            },
            ["path"],
        ),
        handler=filesystem.fs_list_dir,
    ),
    "fs_read_file": BuiltinTool(
        name="fs_read_file",
        description="Lee un fichero de texto dentro de las rutas permitidas.",
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero."},
                "max_chars": {"type": "integer", "default": 12000},
            },
            ["path"],
        ),
        handler=filesystem.fs_read_file,
    ),
    "fs_count_text": BuiltinTool(
        name="fs_count_text",
        description=(
            "Cuenta texto de forma determinista en un fichero permitido. "
            "Usala para contar lineas, apariciones de una frase o lineas exactamente iguales."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero."},
                "text": {"type": "string", "description": "Texto/frase a contar. Opcional."},
                "case_sensitive": {"type": "boolean", "default": True},
            },
            ["path"],
        ),
        handler=filesystem.fs_count_text,
    ),
    "fs_write_file": BuiltinTool(
        name="fs_write_file",
        description=(
            "Crea o sobrescribe un fichero de texto dentro de las rutas permitidas. "
            "No debe usarse para secretos salvo peticion explicita."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero."},
                "content": {"type": "string", "description": "Contenido completo a escribir."},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["path", "content"],
        ),
        handler=filesystem.fs_write_file,
    ),
    "fs_delete_path": BuiltinTool(
        name="fs_delete_path",
        description=(
            "Borra un fichero o directorio dentro de las rutas permitidas. "
            "Requiere confirm=true y confirmacion explicita del usuario."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta a borrar."},
                "recursive": {"type": "boolean", "default": False},
                "confirm": {"type": "boolean", "default": False},
            },
            ["path", "confirm"],
        ),
        handler=filesystem.fs_delete_path,
    ),
    "sensor_read_file": BuiltinTool(
        name="sensor_read_file",
        description=(
            "Lee un fichero de sensores dentro de las rutas permitidas. "
            "Acepta JSON, YAML, CSV o texto key=value y devuelve datos estructurados."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero de sensores."},
                "format": {"type": "string", "enum": ["auto", "json", "yaml", "csv", "keyvalue", "text"], "default": "auto"},
                "source": {"type": "string", "description": "Nombre opcional del origen/sensor."},
            },
            ["path"],
        ),
        handler=filesystem.sensor_read_file,
    ),
    "data_write_file": BuiltinTool(
        name="data_write_file",
        description=(
            "Escribe o anexa datos a un fichero permitido. "
            "Util para exportar observaciones, decisiones, metricas o resultados web."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero destino."},
                "data": {"description": "Dato a guardar: string, objeto o lista."},
                "format": {"type": "string", "enum": ["json", "jsonl", "text"], "default": "json"},
                "append": {"type": "boolean", "default": True},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["path", "data"],
        ),
        handler=filesystem.data_write_file,
    ),
    "web_fetch_url": BuiltinTool(
        name="web_fetch_url",
        description=(
            "Descarga una URL http/https publica y extrae texto basico. "
            "Usala para obtener informacion actual de paginas web o APIs publicas."
        ),
        parameters=object_schema(
            {
                "url": {"type": "string", "description": "URL publica http o https."},
                "max_chars": {"type": "integer", "default": 12000},
                "headers": {"type": "object", "description": "Cabeceras HTTP opcionales."},
            },
            ["url"],
        ),
        handler=web.web_fetch_url,
    ),
}

HOMEASSISTANT_TOOLS: dict[str, BuiltinTool] = {
    "ha_get_states": BuiltinTool(
        name="ha_get_states",
        description="Lista estados actuales de Home Assistant con filtros opcionales por dominio o texto.",
        parameters=object_schema(
            {
                "query": {"type": "string", "description": "Texto a buscar en entidad, nombre, estado o atributos principales."},
                "domain": {"type": "string", "description": "Dominio opcional: sensor, switch, light, binary_sensor, etc."},
                "limit": {"type": "integer", "default": 100},
            }
        ),
        handler=homeassistant.ha_get_states,
    ),
    "ha_get_state": BuiltinTool(
        name="ha_get_state",
        description="Lee el estado completo de una entidad concreta de Home Assistant.",
        parameters=object_schema({"entity_id": {"type": "string", "description": "Entidad, por ejemplo sensor.salon_temperature."}}, ["entity_id"]),
        handler=homeassistant.ha_get_state,
    ),
    "ha_search_entities": BuiltinTool(
        name="ha_search_entities",
        description="Busca entidades de Home Assistant por texto en entity_id, friendly_name, device_class, unidad o estado.",
        parameters=object_schema(
            {
                "query": {"type": "string", "description": "Texto de busqueda, por ejemplo temperatura, salon, puerta."},
                "limit": {"type": "integer", "default": 20},
            },
            ["query"],
        ),
        handler=homeassistant.ha_search_entities,
    ),
    "ha_get_services": BuiltinTool(
        name="ha_get_services",
        description="Lista servicios disponibles de Home Assistant, opcionalmente filtrados por dominio.",
        parameters=object_schema({"domain": {"type": "string", "description": "Dominio opcional: light, switch, climate, media_player, etc."}}),
        handler=homeassistant.ha_get_services,
    ),
    "ha_call_service": BuiltinTool(
        name="ha_call_service",
        description=(
            "Ejecuta un servicio generico de Home Assistant. "
            "Requiere confirm=true si cambia estado o si la configuracion exige confirmacion."
        ),
        parameters=object_schema(
            {
                "domain": {"type": "string", "description": "Dominio del servicio, por ejemplo light o switch."},
                "service": {"type": "string", "description": "Nombre del servicio, por ejemplo turn_on, turn_off, toggle."},
                "service_data": {"type": "object", "description": "Datos del servicio."},
                "target": {"type": "object", "description": "Target HA, por ejemplo {entity_id: light.salon}."},
                "confirm": {"type": "boolean", "default": False},
            },
            ["domain", "service"],
        ),
        handler=homeassistant.ha_call_service,
    ),
    "ha_render_template": BuiltinTool(
        name="ha_render_template",
        description="Renderiza una plantilla Jinja de Home Assistant para consultas avanzadas de estado.",
        parameters=object_schema({"template": {"type": "string", "description": "Plantilla Jinja de Home Assistant."}}, ["template"]),
        handler=homeassistant.ha_render_template,
    ),
    "ha_get_events": BuiltinTool(
        name="ha_get_events",
        description="Lista tipos de eventos disponibles en Home Assistant.",
        parameters=object_schema({}),
        handler=homeassistant.ha_get_events,
    ),
    "ha_get_error_log": BuiltinTool(
        name="ha_get_error_log",
        description="Lee el final del log de errores de Home Assistant.",
        parameters=object_schema({"max_chars": {"type": "integer", "default": 12000}}),
        handler=homeassistant.ha_get_error_log,
    ),
    "ha_get_history": BuiltinTool(
        name="ha_get_history",
        description=(
            "Consulta historico de Home Assistant. Si no sabes la entidad, usa query/domain/device_class "
            "para buscar varias entidades candidatas, por ejemplo query=temperatura, domain=sensor, device_class=temperature."
        ),
        parameters=object_schema(
            {
                "start_time": {"type": "string", "description": "Inicio en ISO 8601."},
                "end_time": {"type": "string", "description": "Fin en ISO 8601."},
                "entity_id": {"type": "string", "description": "Entidad concreta de Home Assistant. Opcional si usas query/domain."},
                "query": {"type": "string", "description": "Texto para buscar entidades candidatas, por ejemplo temperatura."},
                "domain": {"type": "string", "description": "Dominio opcional para buscar candidatos, por defecto sensor."},
                "device_class": {"type": "string", "description": "device_class opcional, por ejemplo temperature."},
                "limit": {"type": "integer", "default": 12},
            },
            ["start_time"],
        ),
        handler=homeassistant.ha_get_history,
    ),
    "ha_get_logbook": BuiltinTool(
        name="ha_get_logbook",
        description="Consulta el logbook historico de Home Assistant para aperturas, movimiento, alarmas o cambios de estado.",
        parameters=object_schema(
            {
                "start_time": {"type": "string", "description": "Inicio en ISO 8601."},
                "end_time": {"type": "string", "description": "Fin en ISO 8601."},
                "entity_id": {"type": "string", "description": "Entidad opcional."},
            },
            ["start_time"],
        ),
        handler=homeassistant.ha_get_logbook,
    ),
}


def builtin_tool_names(*, include_homeassistant: bool = False) -> list[str]:
    names = list(BUILTIN_TOOLS)
    if include_homeassistant:
        names.extend(HOMEASSISTANT_TOOLS)
    return names


def builtin_tool_schemas(*, include_homeassistant: bool = False) -> list[dict[str, Any]]:
    tools = list(BUILTIN_TOOLS.values())
    if include_homeassistant:
        tools.extend(HOMEASSISTANT_TOOLS.values())
    return [tool.openai_schema() for tool in tools]


async def call_builtin_tool(context: Any, name: str, args: dict[str, Any]) -> str:
    tool = BUILTIN_TOOLS.get(name) or HOMEASSISTANT_TOOLS.get(name)
    if tool is None:
        raise ValueError(f"Herramienta interna desconocida: {name}")
    return await tool.handler(context, args)
