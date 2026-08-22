"""Strict JSON and JSON Schema helpers for the tool contract."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from tool_runtime.errors import ToolInputValidationError, ToolObservationError, ToolRegistrationError


def _validate_json_value(value: Any, *, path: str = "$", error_type=ToolInputValidationError) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise error_type(f"{path} yalnızca sonlu JSON sayıları içerebilir.")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise error_type(f"{path} içindeki tüm nesne anahtarları string olmalı.")
            _validate_json_value(child, path=f"{path}.{key}", error_type=error_type)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, path=f"{path}[{index}]", error_type=error_type)
        return
    raise error_type(f"{path} JSON ile uyumlu olmayan bir değer içeriyor: {type(value).__name__}.")


def canonical_json(value: Any, *, error_type=ToolInputValidationError) -> str:
    """Validate a strict JSON value and return deterministic compact JSON."""
    _validate_json_value(value, error_type=error_type)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise error_type(f"Değer strict JSON olarak kodlanamadı: {exc}") from exc


def canonical_object(value: Mapping[str, Any], *, error_type=ToolInputValidationError) -> tuple[str, dict[str, Any]]:
    """Validate a JSON object and return canonical JSON plus a defensive copy."""
    if not isinstance(value, Mapping) or isinstance(value, (str, bytes, bytearray)):
        raise error_type("Tool argümanları üst düzeyde JSON nesnesi olmalı.")
    raw = canonical_json(value, error_type=error_type)
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:  # pragma: no cover - canonical_json guarantees this
        raise error_type(f"JSON nesnesi çözülemedi: {exc}") from exc
    return raw, decoded


def validate_input_schema(input_schema: Any) -> dict[str, Any]:
    """Validate and defensively copy a Draft 2020-12 object schema."""
    if not isinstance(input_schema, Mapping):
        raise ToolRegistrationError("ToolSpec.input_schema bir JSON Schema nesnesi olmalı.")
    schema_copy = dict(input_schema)
    if schema_copy.get("type") != "object":
        raise ToolRegistrationError("ToolSpec.input_schema üst düzey type='object' olmalı.")
    try:
        Draft202012Validator.check_schema(schema_copy)
    except SchemaError as exc:
        raise ToolRegistrationError(f"Geçersiz JSON Schema: {exc.message}") from exc
    try:
        canonical_json(schema_copy, error_type=ToolRegistrationError)
    except ToolRegistrationError:
        raise
    return schema_copy


def validate_arguments(input_schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate arguments with jsonschema and return their canonical copy."""
    canonical, decoded = canonical_object(arguments)
    try:
        Draft202012Validator(input_schema).validate(decoded)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path)
        suffix = f" at {location}" if location else ""
        raise ToolInputValidationError(f"Tool argümanları şemaya uymuyor{suffix}: {exc.message}") from exc
    except SchemaError as exc:  # defensive: registration normally catches this
        raise ToolInputValidationError(f"Tool şeması geçersiz: {exc.message}") from exc
    return canonical, decoded


def validate_observation_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise ToolObservationError("ToolObservation.metadata bir JSON nesnesi olmalı.")
    raw = canonical_json(metadata, error_type=ToolObservationError)
    return json.loads(raw)
