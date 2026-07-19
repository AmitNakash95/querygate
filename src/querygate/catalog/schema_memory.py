"""Deterministic, row-free schema snapshots, fingerprints, and structured diffs."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Iterable, Literal, Optional

import pydantic as pyd
import sqlalchemy as sa


class ObservedForeignKey(pyd.BaseModel):
    column: Annotated[str, pyd.StringConstraints(min_length=1, max_length=256)]
    to_table: Annotated[str, pyd.StringConstraints(min_length=1, max_length=256)]
    to_column: Annotated[str, pyd.StringConstraints(min_length=1, max_length=256)]

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class ObservedIndex(pyd.BaseModel):
    columns: tuple[Annotated[str, pyd.StringConstraints(min_length=1, max_length=256)], ...] = (
        pyd.Field(min_length=1, max_length=2000)
    )
    unique: bool = False

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class ObservedColumn(pyd.BaseModel):
    name: Annotated[str, pyd.StringConstraints(min_length=1, max_length=256)]
    data_type: Annotated[str, pyd.StringConstraints(min_length=1, max_length=1000)]
    nullable: bool
    primary_key: bool = False
    comment_fingerprint: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class ObservedTable(pyd.BaseModel):
    name: Annotated[str, pyd.StringConstraints(min_length=1, max_length=256)]
    columns: tuple[ObservedColumn, ...] = pyd.Field(max_length=2000)
    foreign_keys: tuple[ObservedForeignKey, ...] = pyd.Field(default=(), max_length=4000)
    indexes: tuple[ObservedIndex, ...] = pyd.Field(default=(), max_length=2000)
    comment_fingerprint: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)

    @pyd.model_validator(mode="after")
    def _identities_are_unique(self) -> "ObservedTable":
        column_names = [column.name.casefold() for column in self.columns]
        if len(column_names) != len(set(column_names)):
            raise ValueError("observed column names must be unique case-insensitively")
        return self


def _text_fingerprint(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_payload(connection_id: str, tables: tuple[ObservedTable, ...]) -> dict[str, Any]:
    return {
        "format_version": 1,
        "connection_id": connection_id,
        "tables": [table.model_dump(mode="json") for table in tables],
    }


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class ObservedSchemaSnapshot(pyd.BaseModel):
    """A durable schema-only snapshot. Comments are retained only as hashes."""

    format_version: Literal[1] = 1
    connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    tables: tuple[ObservedTable, ...] = pyd.Field(default=(), max_length=5000)
    fingerprint: str = ""

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)

    @pyd.model_validator(mode="after")
    def _verify_fingerprint(self) -> "ObservedSchemaSnapshot":
        table_names = [table.name.casefold() for table in self.tables]
        if len(table_names) != len(set(table_names)):
            raise ValueError("observed table names must be unique case-insensitively")
        expected = _payload_fingerprint(_canonical_payload(self.connection_id, self.tables))
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("schema snapshot fingerprint does not match its canonical content")
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", expected)
        return self

    @classmethod
    def from_tables(
        cls, connection_id: str, tables: Iterable[sa.Table]
    ) -> "ObservedSchemaSnapshot":
        observed_tables: list[ObservedTable] = []
        for table in sorted(tables, key=lambda value: (value.name.lower(), value.name)):
            columns = tuple(
                ObservedColumn(
                    name=column.name,
                    data_type=str(column.type),
                    nullable=bool(column.nullable),
                    primary_key=bool(column.primary_key),
                    comment_fingerprint=_text_fingerprint(column.comment),
                )
                for column in sorted(
                    table.columns, key=lambda value: (value.name.lower(), value.name)
                )
            )
            foreign_keys = tuple(
                sorted(
                    (
                        ObservedForeignKey(
                            column=foreign_key.parent.name,
                            # target_fullname does not force SQLAlchemy to
                            # resolve/load the referred Table, so a partial
                            # reflection snapshot remains fingerprintable.
                            to_table=foreign_key.target_fullname.split(".")[-2],
                            to_column=foreign_key.target_fullname.split(".")[-1],
                        )
                        for foreign_key in table.foreign_keys
                    ),
                    key=lambda value: (value.column, value.to_table, value.to_column),
                )
            )
            indexes = tuple(
                sorted(
                    (
                        ObservedIndex(
                            columns=tuple(column.name for column in index.columns),
                            unique=bool(index.unique),
                        )
                        for index in table.indexes
                    ),
                    key=lambda value: (value.columns, value.unique),
                )
            )
            observed_tables.append(
                ObservedTable(
                    name=table.name,
                    columns=columns,
                    foreign_keys=foreign_keys,
                    indexes=indexes,
                    comment_fingerprint=_text_fingerprint(table.comment),
                )
            )
        return cls(connection_id=connection_id, tables=tuple(observed_tables))


SchemaChangeKind = Literal[
    "table_added",
    "table_removed",
    "column_added",
    "column_removed",
    "column_changed",
    "relationships_changed",
    "indexes_changed",
    "comment_changed",
]


class SchemaChange(pyd.BaseModel):
    kind: SchemaChangeKind
    table: str
    column: Optional[str] = None
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class PossibleRename(pyd.BaseModel):
    object_type: Literal["table", "column"]
    from_name: str
    to_name: str
    table: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class SchemaDiff(pyd.BaseModel):
    before_fingerprint: str
    after_fingerprint: str
    changed: bool
    changes: tuple[SchemaChange, ...] = ()
    possible_renames: tuple[PossibleRename, ...] = ()

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


def _column_signature(column: ObservedColumn) -> tuple[Any, ...]:
    return (
        column.data_type,
        column.nullable,
        column.primary_key,
        column.comment_fingerprint,
    )


def _table_signature(table: ObservedTable) -> tuple[Any, ...]:
    return (
        tuple((column.name.lower(), _column_signature(column)) for column in table.columns),
        table.foreign_keys,
        table.indexes,
        table.comment_fingerprint,
    )


def _unique_rename_pairs(
    removed: dict[str, Any], added: dict[str, Any], signature
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for old_name, old_value in removed.items():
        matches = [
            new_name
            for new_name, new_value in added.items()
            if signature(old_value) == signature(new_value)
        ]
        if len(matches) != 1:
            continue
        new_name = matches[0]
        reverse = [
            candidate
            for candidate, candidate_value in removed.items()
            if signature(candidate_value) == signature(added[new_name])
        ]
        if len(reverse) == 1:
            pairs.append((old_name, new_name))
    return pairs


def diff_schema_snapshots(
    before: ObservedSchemaSnapshot, after: ObservedSchemaSnapshot
) -> SchemaDiff:
    if before.connection_id != after.connection_id:
        raise ValueError("schema snapshots must belong to the same connection")

    old_tables = {table.name: table for table in before.tables}
    new_tables = {table.name: table for table in after.tables}
    removed_tables = {name: old_tables[name] for name in old_tables.keys() - new_tables.keys()}
    added_tables = {name: new_tables[name] for name in new_tables.keys() - old_tables.keys()}
    changes: list[SchemaChange] = [
        SchemaChange(kind="table_removed", table=name)
        for name in sorted(removed_tables, key=str.lower)
    ]
    changes.extend(
        SchemaChange(kind="table_added", table=name) for name in sorted(added_tables, key=str.lower)
    )
    renames = [
        PossibleRename(object_type="table", from_name=old_name, to_name=new_name)
        for old_name, new_name in _unique_rename_pairs(
            removed_tables, added_tables, _table_signature
        )
    ]

    for table_name in sorted(old_tables.keys() & new_tables.keys(), key=str.lower):
        old_table = old_tables[table_name]
        new_table = new_tables[table_name]
        old_columns = {column.name: column for column in old_table.columns}
        new_columns = {column.name: column for column in new_table.columns}
        removed_columns = {
            name: old_columns[name] for name in old_columns.keys() - new_columns.keys()
        }
        added_columns = {
            name: new_columns[name] for name in new_columns.keys() - old_columns.keys()
        }
        changes.extend(
            SchemaChange(kind="column_removed", table=table_name, column=name)
            for name in sorted(removed_columns, key=str.lower)
        )
        changes.extend(
            SchemaChange(kind="column_added", table=table_name, column=name)
            for name in sorted(added_columns, key=str.lower)
        )
        renames.extend(
            PossibleRename(
                object_type="column",
                table=table_name,
                from_name=old_name,
                to_name=new_name,
            )
            for old_name, new_name in _unique_rename_pairs(
                removed_columns, added_columns, _column_signature
            )
        )
        for column_name in sorted(old_columns.keys() & new_columns.keys(), key=str.lower):
            old_column = old_columns[column_name]
            new_column = new_columns[column_name]
            if _column_signature(old_column) != _column_signature(new_column):
                changes.append(
                    SchemaChange(
                        kind="column_changed",
                        table=table_name,
                        column=column_name,
                        before=old_column.model_dump(mode="json", exclude={"name"}),
                        after=new_column.model_dump(mode="json", exclude={"name"}),
                    )
                )
        if old_table.foreign_keys != new_table.foreign_keys:
            changes.append(SchemaChange(kind="relationships_changed", table=table_name))
        if old_table.indexes != new_table.indexes:
            changes.append(SchemaChange(kind="indexes_changed", table=table_name))
        if old_table.comment_fingerprint != new_table.comment_fingerprint:
            changes.append(SchemaChange(kind="comment_changed", table=table_name))

    return SchemaDiff(
        before_fingerprint=before.fingerprint,
        after_fingerprint=after.fingerprint,
        changed=before.fingerprint != after.fingerprint,
        changes=tuple(changes),
        possible_renames=tuple(renames),
    )
