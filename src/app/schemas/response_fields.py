"""Require selected emitted fields in response schemas without changing nested contracts."""

from typing import ClassVar

from pydantic import BaseModel, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema


class RequiredResponseFields(BaseModel):
    required_response_fields: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = super().__get_pydantic_json_schema__(core_schema, handler)
        if handler.mode == "serialization":
            schema["required"] = list(
                dict.fromkeys([*schema.get("required", []), *cls.required_response_fields])
            )
        return schema
