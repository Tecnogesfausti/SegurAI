from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
import shutil
from pathlib import Path
from typing import Any

from tools.common import compact_json


def resolve_allowed_path(path_text: str, fs_roots: list[Path], *, must_exist: bool = False) -> Path:
    if not path_text.strip():
        raise ValueError("La ruta no puede estar vacia")
    raw_path = Path(path_text).expanduser()
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    if must_exist:
        resolved = raw_path.resolve(strict=True)
    else:
        resolved = raw_path.parent.resolve(strict=True) / raw_path.name

    for root in fs_roots:
        with contextlib.suppress(ValueError):
            resolved.relative_to(root)
            return resolved
        if resolved == root:
            return resolved
    raise PermissionError("Ruta fuera de las raices permitidas: " + ", ".join(str(root) for root in fs_roots))


async def fs_list_dir(context: Any, args: dict[str, Any]) -> str:
    path = resolve_allowed_path(str(args.get("path", ".")), context.fs_roots, must_exist=True)
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    limit = max(1, min(int(args.get("limit", 100) or 100), 500))
    rows: list[dict[str, Any]] = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:limit]:
        stat = child.stat()
        rows.append(
            {
                "name": child.name,
                "path": str(child),
                "type": "dir" if child.is_dir() else "file",
                "size": stat.st_size,
                "modified": dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC).isoformat(timespec="seconds"),
            }
        )
    return compact_json({"path": str(path), "items": rows}, max_chars=16000)


async def fs_read_file(context: Any, args: dict[str, Any]) -> str:
    path = resolve_allowed_path(str(args.get("path", "")), context.fs_roots, must_exist=True)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    max_chars = max(1, min(int(args.get("max_chars", 12000) or 12000), 100000))
    data = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(data) > max_chars
    return compact_json({"path": str(path), "truncated": truncated, "content": data[:max_chars]}, max_chars=max_chars + 1000)


async def fs_count_text(context: Any, args: dict[str, Any]) -> str:
    path = resolve_allowed_path(str(args.get("path", "")), context.fs_roots, must_exist=True)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    data = path.read_text(encoding="utf-8", errors="replace")
    lines = data.splitlines()
    needle = args.get("text")
    case_sensitive = bool(args.get("case_sensitive", True))
    result: dict[str, Any] = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "line_count": len(lines),
        "non_empty_line_count": sum(1 for line in lines if line.strip()),
        "unique_line_count": len(set(lines)),
    }
    if isinstance(needle, str) and needle:
        haystack_text = data if case_sensitive else data.lower()
        haystack_lines = lines if case_sensitive else [line.lower() for line in lines]
        search_text = needle if case_sensitive else needle.lower()
        result.update(
            {
                "text": needle,
                "case_sensitive": case_sensitive,
                "substring_count": haystack_text.count(search_text),
                "exact_line_count": sum(1 for line in haystack_lines if line == search_text),
                "line_contains_count": sum(1 for line in haystack_lines if search_text in line),
            }
        )
    return compact_json(result)


async def fs_write_file(context: Any, args: dict[str, Any]) -> str:
    path = resolve_allowed_path(str(args.get("path", "")), context.fs_roots, must_exist=False)
    content = str(args.get("content", ""))
    overwrite = bool(args.get("overwrite", False))
    if path.exists() and not overwrite:
        raise FileExistsError(f"El fichero ya existe: {path}. Usa overwrite=true para reemplazarlo.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return compact_json({"written": True, "path": str(path), "bytes": path.stat().st_size})


async def fs_delete_path(context: Any, args: dict[str, Any]) -> str:
    path = resolve_allowed_path(str(args.get("path", "")), context.fs_roots, must_exist=True)
    recursive = bool(args.get("recursive", False))
    confirm = bool(args.get("confirm", False))
    if not confirm:
        raise PermissionError("Borrado rechazado: falta confirm=true tras confirmacion explicita del usuario.")
    if path.is_dir():
        if recursive:
            shutil.rmtree(path)
        else:
            path.rmdir()
    else:
        path.unlink()
    return compact_json({"deleted": True, "path": str(path), "recursive": recursive})


async def sensor_read_file(context: Any, args: dict[str, Any]) -> str:
    path = resolve_allowed_path(str(args.get("path", "")), context.fs_roots, must_exist=True)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    format_name = str(args.get("format", "auto") or "auto").lower()
    text = path.read_text(encoding="utf-8", errors="replace")
    data = parse_sensor_file(text, path, format_name, yaml_module=getattr(context, "yaml", None))
    summary = summarize_sensor_payload(data)
    source = str(args.get("source") or path.name)[:80]
    context.memory.add_observation(source=f"file:{source}", summary=summary, raw=compact_json(data, 12000))
    return compact_json({"path": str(path), "source": source, "summary": summary, "data": data, "observation_saved": True}, max_chars=20000)


def parse_sensor_file(text: str, path: Path, format_name: str, *, yaml_module: Any) -> Any:
    if format_name == "auto":
        suffix = path.suffix.lower()
        if suffix == ".json":
            format_name = "json"
        elif suffix in {".yaml", ".yml"}:
            format_name = "yaml"
        elif suffix == ".csv":
            format_name = "csv"
        elif any("=" in line or ":" in line for line in text.splitlines()[:10]):
            format_name = "keyvalue"
        else:
            format_name = "text"
    if format_name == "json":
        return json.loads(text)
    if format_name == "yaml":
        if yaml_module is None:
            raise RuntimeError("PyYAML no esta disponible")
        return yaml_module.safe_load(text)
    if format_name == "csv":
        return list(csv.DictReader(io.StringIO(text)))
    if format_name == "keyvalue":
        data: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            separator = "=" if "=" in line else ":" if ":" in line else None
            if separator is None:
                continue
            key, value = line.split(separator, 1)
            data[key.strip()] = value.strip()
        return data
    if format_name == "text":
        return {"text": text[:20000], "truncated": len(text) > 20000}
    raise ValueError(f"Formato de sensores no soportado: {format_name}")


def summarize_sensor_payload(data: Any) -> str:
    if isinstance(data, dict):
        parts = []
        for key, value in list(data.items())[:20]:
            value_text = compact_json(value, 300) if isinstance(value, (dict, list)) else str(value)
            parts.append(f"{key}={value_text}")
        return "; ".join(parts) or "Fichero de sensores vacio"
    if isinstance(data, list):
        return f"{len(data)} lecturas/filas de sensores"
    return str(data)[:1000] or "Fichero de sensores vacio"


async def data_write_file(context: Any, args: dict[str, Any]) -> str:
    path = resolve_allowed_path(str(args.get("path", "")), context.fs_roots, must_exist=False)
    data = args.get("data")
    format_name = str(args.get("format", "json") or "json").lower()
    append = bool(args.get("append", True))
    overwrite = bool(args.get("overwrite", False))
    if path.exists() and not append and not overwrite:
        raise FileExistsError(f"El fichero ya existe: {path}. Usa append=true u overwrite=true.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "json":
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    elif format_name == "jsonl":
        rows = data if isinstance(data, list) else [data]
        content = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    elif format_name == "text":
        content = data if isinstance(data, str) else compact_json(data, 20000)
        if not content.endswith("\n"):
            content += "\n"
    else:
        raise ValueError(f"Formato de escritura no soportado: {format_name}")
    mode = "a" if append and not overwrite else "w"
    with path.open(mode, encoding="utf-8") as fh:
        fh.write(content)
    return compact_json({"written": True, "path": str(path), "bytes": path.stat().st_size, "mode": mode})
