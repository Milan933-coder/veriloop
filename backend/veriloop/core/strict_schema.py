"""Pydantic model -> JSON Schema accepted by OpenAI-style `response_format` strict mode (D-01a).

Strict mode rejects a schema (upstream code `invalid_json_schema`) unless every object has
`additionalProperties: false` and lists *all* its properties in `required`. Transform rules:
* every property becomes required; fields that are `Optional[...]` in pydantic already carry
  `anyOf [..., {"type": "null"}]`, so the model answers `null` instead of omitting them. Fields
  with a non-null default are NOT made nullable (pydantic would then reject the `null`).
* `$ref` with sibling keywords (e.g. a description) is inlined, `allOf` of one is unwrapped and
  `oneOf` becomes `anyOf`: strict mode supports neither form.
* annotation keywords (`title`, `default`, `examples`) and validation keywords (`minimum`, `pattern`,
  `format`, `minItems` ...) are dropped for portability across upstream models; pydantic still
  validates the response, and the LLM client does one repair round-trip on failure.
* free-form objects (`dict`, `dict[str, X]`) cannot be expressed in strict mode: they become empty
  objects (the model can only answer `{}`) and a warning is logged. Use explicit models instead.
"""

from __future__ import annotations

import copy
from typing import Any

import structlog
from pydantic import BaseModel

log = structlog.get_logger(__name__)

_DROP = frozenset(
    {
        "title",
        "default",
        "examples",
        "discriminator",
        "readOnly",
        "writeOnly",
        "deprecated",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "contentEncoding",
        "contentMediaType",
        "$schema",
    }
)
_ANY_TYPE = {"type": ["string", "number", "integer", "boolean", "null"]}


class _Transformer:
    def __init__(self, defs: dict[str, Any], model_name: str) -> None:
        self.defs = defs
        self.model_name = model_name

    def resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/$defs/"):
            raise ValueError(f"unsupported $ref {ref!r} in {self.model_name}")
        return copy.deepcopy(self.defs[ref.split("/")[-1]])

    def walk(self, node: Any, path: str) -> Any:
        if not isinstance(node, dict):
            return node
        node = dict(node)
        if "$ref" in node and len(node) > 1:
            target = self.resolve(node.pop("$ref"))
            node = {**target, **{k: v for k, v in node.items() if k not in _DROP}}
        if "allOf" in node and len(node["allOf"]) == 1:
            only = node.pop("allOf")[0]
            if "$ref" in only:
                only = self.resolve(only["$ref"])
            node = {**only, **{k: v for k, v in node.items() if k not in _DROP}}
        for key in _DROP & node.keys():
            del node[key]
        if "oneOf" in node:
            node["anyOf"] = node.pop("oneOf")
        if "prefixItems" in node:
            raise ValueError(f"{self.model_name}{path}: tuples are not supported in strict mode")
        if "$ref" in node:
            return node
        if not node:
            log.warning("strict_schema_any_field", model=self.model_name, path=path or "/")
            return dict(_ANY_TYPE)
        if "anyOf" in node:
            node["anyOf"] = [self.walk(sub, f"{path}/anyOf/{i}") for i, sub in enumerate(node["anyOf"])]
        if "allOf" in node:
            node["allOf"] = [self.walk(sub, f"{path}/allOf/{i}") for i, sub in enumerate(node["allOf"])]
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties") or {}
            extra = node.get("additionalProperties")
            if not props and extra not in (None, False):
                log.warning("strict_schema_freeform_object", model=self.model_name, path=path or "/")
            node["type"] = "object"
            node["properties"] = {name: self.walk(sub, f"{path}/{name}") for name, sub in props.items()}
            node["required"] = list(props)
            node["additionalProperties"] = False
        if "items" in node:
            node["items"] = self.walk(node["items"], f"{path}/[]")
        return node


def to_strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for `response_format={"type":"json_schema","json_schema":{"strict":true,...}}`."""
    raw = model.model_json_schema(mode="validation", ref_template="#/$defs/{model}")
    defs = raw.pop("$defs", {})
    tr = _Transformer(defs, model.__name__)
    out = tr.walk(raw, "")
    if out.get("type") != "object":
        raise ValueError(f"{model.__name__}: strict mode needs an object at the root")
    if defs:
        out["$defs"] = {name: tr.walk(d, f"#/$defs/{name}") for name, d in defs.items()}
    return out


def schema_name(task: str) -> str:
    """`json_schema.name` must match ^[a-zA-Z0-9_-]{1,64}$."""
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "_-") else "_" for ch in task)
    return (cleaned or "response")[:64]
