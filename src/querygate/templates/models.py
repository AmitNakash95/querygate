"""Query-template model: a named, parameterized `StructuredQuery` skeleton.

The stored `query` is a raw dict (an *unvalidated* `StructuredQuery` skeleton
with `{"param": "<name>"}` placeholders in value positions), deliberately not a
validated `StructuredQuery`: a placeholder can legitimately sit where a
concrete `StructuredQuery` requires a real value (e.g. a `limit`), so validation
is deferred to *after* binding, when the substituted result is checked as a
real `StructuredQuery` — see `templates/binding.py`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

import pydantic as pyd

# Identifier shape for template ids and parameter names — usable directly as a
# tool/parameter name by an agent framework.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ParameterType = Literal["string", "integer", "number", "boolean"]


class TemplateParameter(pyd.BaseModel):
    """One typed parameter slot of a query template.

    A supplied value is validated against this slot (type, required/default,
    numeric min/max, string max_length, allowed-values enum) before it is bound
    into the query skeleton — see `binding.validate_parameter_value`.
    """

    name: str = pyd.Field(description='Parameter name, referenced as {"param": name} in query.')
    type: ParameterType
    required: bool = True
    description: Optional[str] = None
    # Used when the parameter is not required and the caller omits it. A None
    # default is treated as "no default" (a value must then be supplied).
    default: Optional[Any] = None
    # When true, the value is a non-empty list of `type` (for in/not_in/between
    # style predicates); each element is validated against the same constraints.
    is_list: bool = False
    # Numeric bounds (integer/number only).
    min: Optional[float] = None
    max: Optional[float] = None
    # String length bound (string only).
    max_length: Optional[int] = None
    # Enum of permitted scalar values.
    allowed_values: Optional[List[Any]] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _IDENTIFIER.match(value):
            raise ValueError(f"parameter name must be a valid identifier, got {value!r}")
        return value

    @pyd.model_validator(mode="after")
    def _validate_constraints(self) -> "TemplateParameter":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"parameter {self.name!r}: min {self.min} > max {self.max}")
        if (self.min is not None or self.max is not None) and self.type not in (
            "integer",
            "number",
        ):
            raise ValueError(f"parameter {self.name!r}: min/max only valid for numeric types")
        if self.max_length is not None and self.type != "string":
            raise ValueError(f"parameter {self.name!r}: max_length only valid for string type")
        if self.max_length is not None and self.max_length < 0:
            raise ValueError(f"parameter {self.name!r}: max_length must be non-negative")
        return self


class QueryTemplate(pyd.BaseModel):
    """A named, parameterized query. `query` is the raw skeleton (see module
    docstring); `connection` is the connection it targets and the visibility
    boundary — a caller may see/invoke the template only if that connection is
    visible to its principal.
    """

    id: str = pyd.Field(description="Unique template id, referenced when invoking it.")
    connection: str = pyd.Field(description="Target connection id.")
    description: Optional[str] = None
    parameters: List[TemplateParameter] = pyd.Field(default_factory=list)
    query: Dict[str, Any] = pyd.Field(
        description='StructuredQuery skeleton with {"param": name} placeholders.'
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("id", "connection")
    @classmethod
    def _valid_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @pyd.field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _IDENTIFIER.match(value):
            raise ValueError(f"template id must be a valid identifier, got {value!r}")
        return value

    @pyd.model_validator(mode="after")
    def _unique_parameter_names(self) -> "QueryTemplate":
        names = [p.name.casefold() for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError(f"template {self.id!r} has duplicate parameter names")
        return self

    def parameter(self, name: str) -> Optional[TemplateParameter]:
        for param in self.parameters:
            if param.name == name:
                return param
        return None


class QueryTemplateFile(pyd.BaseModel):
    """Top-level shape of the templates YAML file: a list of templates."""

    templates: List[QueryTemplate] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _unique_ids(self) -> "QueryTemplateFile":
        ids = [t.id.casefold() for t in self.templates]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate template id in templates file")
        return self


class PublicTemplateParameter(pyd.BaseModel):
    """Agent-facing projection of a parameter slot — the callable signature."""

    name: str
    type: ParameterType
    required: bool
    description: Optional[str] = None
    is_list: bool = False
    min: Optional[float] = None
    max: Optional[float] = None
    max_length: Optional[int] = None
    allowed_values: Optional[List[Any]] = None

    model_config = pyd.ConfigDict(extra="forbid")


class PublicQueryTemplate(pyd.BaseModel):
    """Agent-facing projection of a template: name + signature, not the query
    skeleton itself (an agent calls the tool by name, it doesn't need the AST).
    """

    id: str
    connection: str
    description: Optional[str] = None
    parameters: List[PublicTemplateParameter] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")

    @classmethod
    def from_template(cls, template: QueryTemplate) -> "PublicQueryTemplate":
        return cls(
            id=template.id,
            connection=template.connection,
            description=template.description,
            parameters=[
                PublicTemplateParameter(
                    name=p.name,
                    type=p.type,
                    required=p.required,
                    description=p.description,
                    is_list=p.is_list,
                    min=p.min,
                    max=p.max,
                    max_length=p.max_length,
                    allowed_values=p.allowed_values,
                )
                for p in template.parameters
            ],
        )
