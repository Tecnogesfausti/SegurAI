from __future__ import annotations

import datetime as dt
import math
import re
import xml.etree.ElementTree as ET
from typing import Any
from zoneinfo import ZoneInfo

from services.live_context.config import LiveContextConfig
from services.live_context.http_client import LiveContextHTTPClient
from services.live_context.models import NormalizedContext
from services.live_context.providers.base import LiveContextProvider


class DGTTrafficProvider(LiveContextProvider):
    domain = "traffic"
    source = "dgt"
    ttl_seconds = 300

    def __init__(self, http_client: LiveContextHTTPClient | None = None) -> None:
        self.http_client = http_client or LiveContextHTTPClient()

    async def fetch(self, config: LiveContextConfig) -> dict[str, Any]:
        location = config.location
        now = dt.datetime.now(ZoneInfo(location.timezone))
        expires_at = now + dt.timedelta(seconds=self.ttl_seconds)
        url = getattr(config, "dgt_traffic_url", "")
        warnings: list[str] = []
        if not url:
            warnings.append("dgt_traffic_url_missing")
            return NormalizedContext(
                domain=self.domain,
                location=location,
                source=self.source,
                fetched_at=now,
                expires_at=expires_at,
                is_stale=False,
                confidence=0.2,
                summary="Trafico DGT no configurado: falta DGT_TRAFFIC_URL.",
                data={"incidents": []},
                warnings=warnings,
            ).as_dict()

        payload = await self._fetch_payload(url)
        incidents = normalize_incidents(payload, location.lat, location.lon)
        nearby = [item for item in incidents if item.get("distance_km") is None or item["distance_km"] <= location.radius_km]
        nearby.sort(key=lambda item: (item.get("distance_km") is None, item.get("distance_km") or 9999))
        summary = build_summary(nearby)
        return NormalizedContext(
            domain=self.domain,
            location=location,
            source=self.source,
            fetched_at=now,
            expires_at=expires_at,
            is_stale=False,
            confidence=0.75,
            summary=summary,
            data={"incidents": nearby[:50], "source_url": url},
            warnings=warnings,
        ).as_dict()

    async def _fetch_payload(self, url: str) -> Any:
        if url.lower().endswith(('.xml', '.datex', '.datex2')):
            text = await self.http_client.get_text(url)
            return parse_xml_payload(text)
        try:
            return await self.http_client.get_json(url)
        except Exception:
            text = await self.http_client.get_text(url)
            if text.lstrip().startswith("<"):
                return parse_xml_payload(text)
            raise


def normalize_incidents(payload: Any, lat: float, lon: float) -> list[dict[str, Any]]:
    rows = extract_rows(payload)
    incidents: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        incident = normalize_incident(row, lat, lon)
        if incident:
            incidents.append(incident)
    return incidents


def extract_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("incidents", "incidencias", "situations", "situationRecords", "records", "results", "data", "features"):
        value = payload.get(key)
        if isinstance(value, list):
            if key == "features":
                return [feature.get("properties", {}) | extract_geometry(feature) for feature in value if isinstance(feature, dict)]
            return value
    return [payload]


def extract_geometry(feature: dict[str, Any]) -> dict[str, Any]:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if isinstance(coordinates, list) and len(coordinates) >= 2:
        return {"lon": coordinates[0], "lat": coordinates[1]}
    return {}


def normalize_incident(row: dict[str, Any], lat: float, lon: float) -> dict[str, Any] | None:
    road = first_text(row, "road", "roadName", "carretera", "via", "codigoCarretera", "nombreCarretera")
    title = first_text(row, "title", "titulo", "headline", "eventType", "tipo", "type") or "Incidencia de trafico"
    description = first_text(row, "description", "descripcion", "comment", "comments", "cause", "causa", "observations") or title
    severity = normalize_severity(first_text(row, "severity", "gravedad", "level", "nivel", "impact") or description)
    event_type = first_text(row, "event_type", "eventType", "tipo", "type", "subtype")
    incident_lat = first_float(row, "lat", "latitude", "latitud", "y")
    incident_lon = first_float(row, "lon", "lng", "longitude", "longitud", "x")
    distance = haversine_km(lat, lon, incident_lat, incident_lon) if incident_lat is not None and incident_lon is not None else None
    return {
        "id": first_text(row, "id", "identifier", "uid", "situationRecordId") or stable_id(road, title, description),
        "title": clean_text(title),
        "description": clean_text(description),
        "road": normalize_road(road) if road else None,
        "raw_road": road,
        "severity": severity,
        "event_type": clean_text(event_type) if event_type else None,
        "lat": incident_lat,
        "lon": incident_lon,
        "distance_km": round(distance, 1) if distance is not None else None,
        "started_at": first_text(row, "startTime", "start_time", "fechaInicio", "started_at"),
        "updated_at": first_text(row, "updateTime", "updated", "fechaActualizacion", "updated_at"),
    }


def parse_xml_payload(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    records: list[dict[str, Any]] = []
    for element in root.iter():
        tag = strip_namespace(element.tag).lower()
        if tag in {"situationrecord", "incidencia", "incident"}:
            row: dict[str, Any] = {}
            for child in element.iter():
                child_tag = strip_namespace(child.tag)
                if child is element:
                    continue
                value = (child.text or "").strip()
                if value and child_tag not in row:
                    row[child_tag] = value
            if row:
                records.append(row)
    if not records:
        records = [xml_flatten(root)]
    return {"records": records}


def xml_flatten(root: ET.Element) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for child in root.iter():
        value = (child.text or "").strip()
        if value:
            row.setdefault(strip_namespace(child.tag), value)
    return row


def strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_text(row: dict[str, Any], *keys: str) -> str | None:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def first_float(row: dict[str, Any], *keys: str) -> float | None:
    text = first_text(row, *keys)
    if text is None:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def normalize_road(value: str | None) -> str | None:
    if not value:
        return None
    text = clean_text(value).upper().replace(" ", "")
    match = re.search(r"\b([A-Z]{1,3})[- ]?(\d{1,4})\b", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return text or None


def normalize_severity(value: str) -> str:
    text = value.lower()
    if any(term in text for term in ("corte", "cortada", "closed", "accidente", "accident", "muy grave", "severe")):
        return "critical"
    if any(term in text for term in ("retencion", "retención", "congestion", "congestión", "obra", "works", "moderate", "lento")):
        return "warning"
    return "info"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stable_id(*parts: Any) -> str:
    text = "|".join(clean_text(part).lower() for part in parts if part)
    return str(abs(hash(text)))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_summary(incidents: list[dict[str, Any]]) -> str:
    if not incidents:
        return "Sin incidencias de trafico cercanas en la fuente configurada."
    roads: dict[str, int] = {}
    for item in incidents:
        road = item.get("road") or "sin_via"
        roads[road] = roads.get(road, 0) + 1
    top = ", ".join(f"{road}:{count}" for road, count in sorted(roads.items())[:5])
    return f"{len(incidents)} incidencias de trafico cercanas. Vias: {top}."
