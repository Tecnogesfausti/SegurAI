from __future__ import annotations

import re
from typing import Any

from tools.common import compact_json


def ha_base_url(context: Any) -> str:
    base_url = getattr(context, "ha_base_url", None)
    if not base_url:
        raise ValueError("Falta URL base de Home Assistant")
    return str(base_url).rstrip("/")


def ha_headers(context: Any) -> dict[str, str]:
    token = getattr(context, "ha_token", None)
    if not token:
        raise ValueError("Falta HA_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def httpx_module(context: Any) -> Any:
    httpx = getattr(context, "httpx", None)
    if httpx is None:
        raise RuntimeError("httpx no esta disponible")
    return httpx


async def ha_get_states(context: Any, args: dict[str, Any]) -> str:
    query = str(args.get("query", "") or "").strip().lower()
    domain = str(args.get("domain", "") or "").strip().lower()
    limit = max(1, min(int(args.get("limit", 100) or 100), 500))
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/states", headers=ha_headers(context))
        response.raise_for_status()
        states = response.json()

    terms = [term for term in re.split(r"\W+", query) if term]
    rows: list[dict[str, Any]] = []
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if domain and not entity_id.startswith(f"{domain}."):
            continue
        attrs = state.get("attributes") or {}
        haystack = " ".join(
            str(part).lower()
            for part in (
                entity_id,
                attrs.get("friendly_name", ""),
                attrs.get("device_class", ""),
                attrs.get("unit_of_measurement", ""),
                state.get("state", ""),
            )
        )
        if terms and not all(term in haystack for term in terms):
            continue
        rows.append(
            {
                "entity_id": entity_id,
                "state": state.get("state"),
                "friendly_name": attrs.get("friendly_name"),
                "device_class": attrs.get("device_class"),
                "unit": attrs.get("unit_of_measurement"),
                "last_changed": state.get("last_changed"),
                "last_updated": state.get("last_updated"),
            }
        )
        if len(rows) >= limit:
            break
    return compact_json(rows, max_chars=20000)


async def ha_get_state(context: Any, args: dict[str, Any]) -> str:
    entity_id = str(args.get("entity_id", "") or "").strip()
    if not entity_id:
        raise ValueError("entity_id es obligatorio")
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/states/{entity_id}", headers=ha_headers(context))
        response.raise_for_status()
        return compact_json(response.json(), max_chars=16000)


async def ha_search_entities(context: Any, args: dict[str, Any]) -> str:
    return await ha_get_states(context, {"query": args.get("query", ""), "limit": args.get("limit", 20)})


async def ha_get_services(context: Any, args: dict[str, Any]) -> str:
    domain = str(args.get("domain", "") or "").strip().lower()
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/services", headers=ha_headers(context))
        response.raise_for_status()
        services = response.json()
    if domain:
        services = [item for item in services if str(item.get("domain", "")).lower() == domain]
    return compact_json(services, max_chars=24000)


async def ha_call_service(context: Any, args: dict[str, Any]) -> str:
    domain = str(args.get("domain", "") or "").strip()
    service = str(args.get("service", "") or "").strip()
    if not domain or not service:
        raise ValueError("domain y service son obligatorios")
    confirm = bool(args.get("confirm", False))
    if getattr(context, "require_action_confirmation", True) and not confirm:
        raise PermissionError("Accion rechazada: falta confirm=true tras confirmacion explicita del usuario.")
    payload: dict[str, Any] = {}
    service_data = args.get("service_data")
    target = args.get("target")
    if isinstance(service_data, dict):
        payload.update(service_data)
    if isinstance(target, dict):
        payload["target"] = target
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            f"{ha_base_url(context)}/api/services/{domain}/{service}",
            headers=ha_headers(context),
            json=payload,
        )
        response.raise_for_status()
        data = response.json() if response.content else {"ok": True}
    return compact_json({"called": True, "domain": domain, "service": service, "response": data}, max_chars=20000)


async def ha_render_template(context: Any, args: dict[str, Any]) -> str:
    template = str(args.get("template", "") or "")
    if not template.strip():
        raise ValueError("template es obligatorio")
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(
            f"{ha_base_url(context)}/api/template",
            headers=ha_headers(context),
            json={"template": template},
        )
        response.raise_for_status()
        return compact_json({"result": response.text}, max_chars=12000)


async def ha_get_events(context: Any, args: dict[str, Any]) -> str:
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/events", headers=ha_headers(context))
        response.raise_for_status()
        return compact_json(response.json(), max_chars=16000)


async def ha_get_error_log(context: Any, args: dict[str, Any]) -> str:
    max_chars = max(1000, min(int(args.get("max_chars", 12000) or 12000), 50000))
    httpx = httpx_module(context)
    headers = ha_headers(context)
    headers["Accept"] = "text/plain, application/json"
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/error_log", headers=headers)
        response.raise_for_status()
        text = response.text
    return compact_json({"truncated": len(text) > max_chars, "text": text[-max_chars:]}, max_chars=max_chars + 1000)


async def ha_get_history(context: Any, args: dict[str, Any]) -> str:
    start_time = str(args.get("start_time", "") or "").strip()
    if not start_time:
        raise ValueError("start_time es obligatorio")
    end_time = str(args.get("end_time", "") or "").strip()
    entity_id = str(args.get("entity_id", "") or "").strip()
    entity_ids: list[str]
    if entity_id:
        entity_ids = [entity_id]
    else:
        entity_ids = await resolve_history_entities(context, args)
    if not entity_ids:
        return compact_json({"entities": [], "message": "No se encontraron entidades candidatas para consultar historico."})

    httpx = httpx_module(context)
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as http:
        for candidate in entity_ids:
            params: dict[str, str] = {
                "minimal_response": "1",
                "no_attributes": "1",
                "filter_entity_id": candidate,
            }
            if end_time:
                params["end_time"] = end_time
            response = await http.get(
                f"{ha_base_url(context)}/api/history/period/{start_time}",
                headers=ha_headers(context),
                params=params,
            )
            response.raise_for_status()
            history = response.json()
            points = flatten_history_points(history)
            useful_points = useful_history_points(points)
            rows.append(
                {
                    "entity_id": candidate,
                    "points": len(points),
                    "useful_points": len(useful_points),
                    "first": useful_points[0] if useful_points else points[0] if points else None,
                    "last": useful_points[-1] if useful_points else points[-1] if points else None,
                    "sample": useful_points[: min(5, len(useful_points))] or points[: min(5, len(points))],
                }
            )
    return compact_json(
        {
            "start_time": start_time,
            "end_time": end_time or None,
            "entities_checked": len(entity_ids),
            "entities_with_data": sum(1 for row in rows if row["useful_points"]),
            "results": rows,
        },
        max_chars=24000,
    )


async def resolve_history_entities(context: Any, args: dict[str, Any]) -> list[str]:
    query = str(args.get("query", "") or "").strip().lower()
    domain = str(args.get("domain", "sensor") or "sensor").strip().lower()
    device_class = str(args.get("device_class", "") or "").strip().lower()
    limit = max(1, min(int(args.get("limit", 12) or 12), 50))
    states_raw = await ha_get_states(context, {"query": query, "domain": domain, "limit": 500})
    import json

    states = json.loads(states_raw)
    candidates: list[str] = []
    for state in states:
        if device_class and str(state.get("device_class") or "").lower() != device_class:
            continue
        entity_id = str(state.get("entity_id") or "")
        if entity_id and entity_id not in candidates:
            candidates.append(entity_id)
        if len(candidates) >= limit:
            break
    return candidates


def useful_history_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [point for point in points if str(point.get("state", "")).lower() not in {"unknown", "unavailable"}]


def flatten_history_points(history: Any) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    if not isinstance(history, list):
        return points
    for entity_history in history:
        if not isinstance(entity_history, list):
            continue
        for item in entity_history:
            if not isinstance(item, dict):
                continue
            points.append(
                {
                    "state": item.get("state"),
                    "last_changed": item.get("last_changed"),
                    "last_updated": item.get("last_updated"),
                }
            )
    return points


async def ha_get_logbook(context: Any, args: dict[str, Any]) -> str:
    start_time = str(args.get("start_time", "") or "").strip()
    if not start_time:
        raise ValueError("start_time es obligatorio")
    params: dict[str, str] = {}
    end_time = str(args.get("end_time", "") or "").strip()
    entity_id = str(args.get("entity_id", "") or "").strip()
    if end_time:
        params["end_time"] = end_time
    if entity_id:
        params["entity"] = entity_id
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/logbook/{start_time}", headers=ha_headers(context), params=params)
        response.raise_for_status()
        return compact_json(response.json(), max_chars=12000)
