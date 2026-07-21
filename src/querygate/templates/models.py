"""Query-template model: a named, parameterized `StructuredQuery` skeleton.

The stored `query` is a raw dict (an *unvalidated* `StructuredQuery` skeleton
with `{"param": "<name>"}` placeholders in value positions), deliberately not a
validated `StructuredQuery`: a placeholder can legitimately sit where a
concrete `StructuredQuery` requires a real value (e.g. a `limit`), so validation
is deferred to *after* binding, when the substituted result is checked as a
real `StructuredQuery` — see `templates/binding.py`.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Literal, Optional

import pydantic as pyd

# Identifier shape for template ids and parameter names — usable directly as a
# tool/parameter name by an agent framework.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ParameterType = Literal["string", "integer", "number", "boolean"]


def scalar_type_error(
    param_type: ParameterType,
    value: Any,
    *,
    min: Optional[float] = None,
    max: Optional[float] = None,
    max_length: Optional[int] = None,
) -> Optional[str]:
    """Return a human-readable reason `value` is not a valid scalar for
    `param_type` (and its numeric/length bounds), or None if it is fine.

    Single source of truth shared by two callers so they can never disagree
    about what a slot accepts: `TemplateParameter`'s own load-time
    self-consistency check (are the template author's `allowed_values`/`default`
    valid for the declared type?) and `binding._coerce_scalar`'s run-time check
    (is the *invoker's* supplied value valid?). Membership in `allowed_values`
    is deliberately NOT checked here — that is a separate concern each caller
    layers on top.
    """
    if param_type == "boolean":
        return None if isinstance(value, bool) else "must be a boolean"
    # bool is a subclass of int — exclude it from numeric/string types.
    if isinstance(value, bool):
        return f"must be of type {param_type}"
    if param_type == "string":
        if not isinstance(value, str):
            return "must be a string"
        if max_length is not None and len(value) > max_length:
            return f"exceeds max length {max_length}"
    elif param_type == "integer":
        if not isinstance(value, int):
            return "must be an integer"
    elif param_type == "number":
        if not isinstance(value, (int, float)):
            return "must be a number"
        if not math.isfinite(value):
            return "must be finite"
    if param_type in ("integer", "number"):
        if min is not None and value < min:
            return f"is below min {min}"
        if max is not None and value > max:
            return f"is above max {max}"
    return None


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
        self._validate_slot_self_consistency()
        return self

    def _validate_slot_self_consistency(self) -> None:
        """A slot's own `allowed_values` and `default` must be valid for its
        declared type and bounds — otherwise the template deploys but can never
        be invoked (e.g. type `integer` with string `allowed_values`, so every
        integer a caller supplies fails the enum). Caught at load/dry-run time
        rather than surfacing as a confusing run-time rejection.
        """
        if self.allowed_values is not None:
            for value in self.allowed_values:
                reason = scalar_type_error(
                    self.type, value, min=self.min, max=self.max, max_length=self.max_length
                )
                if reason is not None:
                    raise ValueError(
                        f"parameter {self.name!r}: allowed value {value!r} {reason} "
                        f"(declared type is {self.type})"
                    )
        if self.default is not None:
            if self.is_list:
                if not isinstance(self.default, list):
                    raise ValueError(
                        f"parameter {self.name!r}: default for a list parameter must be a list"
                    )
                default_values = self.default
            else:
                default_values = [self.default]
            for value in default_values:
                reason = scalar_type_error(
                    self.type, value, min=self.min, max=self.max, max_length=self.max_length
                )
                if reason is not None:
                    raise ValueError(f"parameter {self.name!r}: default {value!r} {reason}")
                if self.allowed_values is not None and value not in self.allowed_values:
                    raise ValueError(
                        f"parameter {self.name!r}: default {value!r} is not in allowed_values"
                    )


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
