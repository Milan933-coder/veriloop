from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from veriloop.contracts import (
    ClaimExtraction,
    DebateResult,
    EvidenceItem,
    JudgeOutput,
    Verdict,
    VerdictLabel,
    VerifierOutput,
)
from veriloop.core.strict_schema import schema_name, to_strict_schema

FORBIDDEN = {"default", "title", "oneOf", "minimum", "maximum", "format", "examples"}


def assert_strict(node: Any, defs: dict[str, Any], path: str = "") -> None:
    """Walk the schema and check OpenAI strict-mode invariants everywhere."""
    if isinstance(node, list):
        for i, sub in enumerate(node):
            assert_strict(sub, defs, f"{path}[{i}]")
        return
    if not isinstance(node, dict):
        return
    assert not (FORBIDDEN & node.keys()), f"{path}: forbidden keys {FORBIDDEN & node.keys()}"
    if "$ref" in node:
        assert set(node) == {"$ref"}, f"{path}: $ref with siblings"
        assert node["$ref"].split("/")[-1] in defs, f"{path}: dangling {node['$ref']}"
    if node.get("type") == "object":
        assert node.get("additionalProperties") is False, f"{path}: additionalProperties"
        assert node.get("required") == list(node.get("properties", {})), f"{path}: required != properties"
    for key, sub in node.items():
        if key == "properties":  # a name -> schema mapping, not a schema (a field may be named "title")
            for name, prop in sub.items():
                assert_strict(prop, defs, f"{path}/properties/{name}")
        elif key != "$defs":
            assert_strict(sub, defs, f"{path}/{key}")


@pytest.mark.parametrize(
    "model", [ClaimExtraction, JudgeOutput, VerifierOutput, Verdict, EvidenceItem, DebateResult]
)
def test_contract_models_transform_to_valid_strict_schemas(model: type[BaseModel]) -> None:
    schema = to_strict_schema(model)
    defs = schema.get("$defs", {})
    assert schema["type"] == "object"
    assert_strict(schema, defs)
    for name, d in defs.items():
        assert_strict(d, defs, f"#/$defs/{name}")


def test_optional_fields_are_nullable_and_required_but_defaults_not_nullable() -> None:
    class Item(BaseModel):
        name: str
        note: str | None = None
        tags: list[str] = []
        score: float = Field(0.5, ge=0, le=1, description="0..1")

    class Out(BaseModel):
        label: Literal["a", "b"]
        verdict: VerdictLabel
        items: list[Item]
        best: Item | None = None
        meta: dict[str, str] = {}

    schema = to_strict_schema(Out)
    props = schema["properties"]
    assert schema["required"] == ["label", "verdict", "items", "best", "meta"]
    assert props["label"]["enum"] == ["a", "b"]
    assert {"type": "null"} in props["best"]["anyOf"]
    assert props["meta"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    item = schema["$defs"]["Item"]
    assert item["required"] == ["name", "note", "tags", "score"]
    assert {"type": "null"} in item["properties"]["note"]["anyOf"]
    assert item["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert item["properties"]["score"] == {"type": "number", "description": "0..1"}  # no null, no bounds
    assert_strict(schema, schema["$defs"])


def test_ref_with_description_is_inlined() -> None:
    class Inner(BaseModel):
        x: int

    class Outer(BaseModel):
        inner: Inner = Field(description="the inner thing")

    schema = to_strict_schema(Outer)
    inner = schema["properties"]["inner"]
    assert "$ref" not in inner and inner["description"] == "the inner thing"
    assert inner["properties"] == {"x": {"type": "integer"}} and inner["additionalProperties"] is False


def test_model_output_round_trips_through_pydantic() -> None:
    # What a strict-mode model would emit for JudgeOutput (all keys present, nullables as null).
    emitted = {
        "label": "FALSE",
        "rationale": [{"text": "Reuters reported X.", "evidence_ids": ["E1", "E2"]}],
        "provider": "aicredits",
        "model_id": "m",
        "extractive": False,
    }
    assert JudgeOutput.model_validate(emitted).label is VerdictLabel.FALSE


def test_schema_name_sanitised() -> None:
    assert schema_name("extract claims/v2!") == "extract_claims_v2_"
    assert schema_name("") == "response" and len(schema_name("x" * 100)) == 64
