"""Schema parity: SQLAlchemy metadata vs. supabase/migrations/*.sql.

W2 keeps the Postgres schema in ``supabase/migrations/`` as the single source
of truth.  This test parses the migrations with stdlib regexes only (no server
and no third-party SQL parser) and fails on any drift from ``backend.models``.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from backend.models import Base

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
PILOT_SCHEMA_FILE = MIGRATIONS_DIR / "20260930120000_pilot_schema.sql"

# SQL-only support tables are allowed; anything else must have an ORM model.
KNOWN_SQL_ONLY_TABLES = {"app_users", "schema_migrations"}
EXPECTED_PILOT_SCHEMA_TABLES = 31

_CREATE_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?P<first>"(?:[^"]|"")*"|[A-Za-z_][\w$]*)'
    r'(?:\s*\.\s*(?P<second>"(?:[^"]|"")*"|[A-Za-z_][\w$]*))?'
    r"\s*\(",
    re.IGNORECASE,
)
_CONSTRAINT_STARTERS = {
    "CONSTRAINT",
    "PRIMARY",
    "UNIQUE",
    "FOREIGN",
    "CHECK",
    "EXCLUDE",
    "LIKE",
}


def _unquote(identifier: str) -> str:
    if identifier.startswith('"') and identifier.endswith('"'):
        return identifier[1:-1].replace('""', '"')
    return identifier


def strip_sql_comments(text: str) -> str:
    """Return *text* with comments blanked out, preserving newlines.

    Single-quoted strings, quoted identifiers and dollar-quoted blocks are not
    treated as comments; ``--`` and ``/* ... */`` inside them are kept.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_single = in_double = False
    while i < n:
        char = text[i]
        if in_single:
            out.append(char)
            if char == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if in_double:
            out.append(char)
            if char == '"':
                in_double = False
            i += 1
            continue
        if char == "'":
            in_single = True
            out.append(char)
            i += 1
            continue
        if char == '"':
            in_double = True
            out.append(char)
            i += 1
            continue
        if char == "$":
            tag_match = re.match(r"\$[A-Za-z_][\w$]*\$|\$\$", text[i:])
            if tag_match:
                tag = tag_match.group(0)
                end = text.find(tag, i + len(tag))
                if end == -1:
                    out.append(text[i:])
                    break
                out.append(text[i : end + len(tag)])
                i = end + len(tag)
                continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            if end == -1:
                break
            out.append("\n")
            i = end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                break
            chunk = text[i : end + 2]
            out.append("".join(ch if ch == "\n" else " " for ch in chunk))
            i = end + 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _extract_parenthesized(text: str, open_index: int) -> tuple[str, int]:
    """Return the body inside the parenthesis at *open_index* and its end index."""
    depth = 0
    i, n = open_index, len(text)
    while i < n:
        char = text[i]
        if char == "'":
            i += 1
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
        elif char == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i], i
        i += 1
    raise ValueError("unbalanced CREATE TABLE parentheses")


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(body)
    while i < n:
        char = body[i]
        if char == "'":
            j = i + 1
            while j < n:
                if body[j] == "'":
                    if j + 1 < n and body[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            buf.append(body[i : j + 1])
            i = j + 1
            continue
        if char == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 1
            buf.append(body[i : j + 1])
            i = j + 1
            continue
        if char == "(":
            depth += 1
            buf.append(char)
        elif char == ")":
            depth -= 1
            buf.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
        i += 1
    parts.append("".join(buf))
    return parts


def _first_identifier(definition: str) -> str | None:
    definition = definition.strip()
    if not definition:
        return None
    if definition.startswith('"'):
        i = 1
        while i < len(definition):
            if definition[i] == '"':
                if i + 1 < len(definition) and definition[i + 1] == '"':
                    i += 2
                    continue
                return definition[1:i].replace('""', '"')
            i += 1
        return None
    match = re.match(r"[A-Za-z_][\w$]*", definition)
    return match.group(0) if match else None


def parse_create_tables(sql_text: str) -> list[tuple[str | None, str, list[str]]]:
    """Parse CREATE TABLE statements into ``[(schema, table, columns), ...]``."""
    clean = strip_sql_comments(sql_text)
    tables: list[tuple[str | None, str, list[str]]] = []
    for match in _CREATE_TABLE_RE.finditer(clean):
        first = _unquote(match.group("first"))
        second = match.group("second")
        schema = None if second is None else first
        table = first if second is None else _unquote(second)
        body, _ = _extract_parenthesized(clean, match.end() - 1)
        columns: list[str] = []
        for definition in _split_top_level(body):
            name = _first_identifier(definition)
            if name is None:
                continue
            if name.split(".")[0].upper() in _CONSTRAINT_STARTERS:
                continue
            columns.append(name)
        tables.append((schema, table, columns))
    return tables


class SchemaParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not cls.migration_files:
            raise AssertionError(f"no migrations found under {MIGRATIONS_DIR}")
        cls.sql_tables: dict[str, list[str]] = {}
        cls.sql_table_files: dict[str, str] = {}
        cls.create_tables: list[tuple[str, str | None, str, list[str]]] = []
        for path in cls.migration_files:
            for schema, table, columns in parse_create_tables(path.read_text(encoding="utf-8")):
                cls.create_tables.append((path.name, schema, table, columns))
                if table in cls.sql_tables:
                    raise AssertionError(
                        f"table {table!r} is defined more than once in supabase/migrations "
                        f"({cls.sql_table_files[table]} and {path.name})"
                    )
                cls.sql_tables[table] = columns
                cls.sql_table_files[table] = path.name
        cls.model_tables = {table.name: table for table in Base.metadata.tables.values()}

    def test_model_tables_exist_with_same_columns(self) -> None:
        if not self.model_tables:
            self.fail("Base.metadata has no tables; backend.models failed to define models")
        for table_name, table in sorted(self.model_tables.items()):
            with self.subTest(table=table_name):
                if table_name not in self.sql_tables:
                    self.fail(
                        f"{table_name!r} is in Base.metadata but missing from "
                        f"supabase/migrations/; known tables: {sorted(self.sql_tables)}"
                    )
                model_columns = [column.name for column in table.columns]
                sql_columns = self.sql_tables[table_name]
                missing = sorted(set(model_columns) - set(sql_columns))
                extra = sorted(set(sql_columns) - set(model_columns))
                if missing or extra:
                    self.fail(
                        f"{table_name!r} column mismatch: model-only={missing}, "
                        f"migration-only={extra}; model={model_columns}; sql={sql_columns}"
                    )

    def test_sql_only_tables_are_known_extras(self) -> None:
        sql_only = sorted(set(self.sql_tables) - set(self.model_tables))
        unexpected = sorted(set(sql_only) - KNOWN_SQL_ONLY_TABLES)
        self.assertFalse(
            unexpected,
            f"unexpected SQL-only tables {unexpected}; add a model or an explicit "
            f"entry to KNOWN_SQL_ONLY_TABLES (currently {sorted(KNOWN_SQL_ONLY_TABLES)})",
        )
        for required in ("app_users", "schema_migrations"):
            self.assertIn(required, sql_only + list(self.model_tables), f"{required} is not defined anywhere")

    def test_pilot_schema_has_31_ddl_tables(self) -> None:
        tables = parse_create_tables(PILOT_SCHEMA_FILE.read_text(encoding="utf-8"))
        self.assertEqual(
            len(tables),
            EXPECTED_PILOT_SCHEMA_TABLES,
            f"{PILOT_SCHEMA_FILE.name} defines {len(tables)} tables, expected "
            f"{EXPECTED_PILOT_SCHEMA_TABLES}; names={[table for _, table, _ in tables]}",
        )
        for _, table, columns in tables:
            self.assertTrue(columns, f"{table} has no parsed columns")

    def test_every_create_table_is_pilot_qualified(self) -> None:
        offenders = [
            f"{filename}: {schema + '.' if schema else ''}{table}"
            for filename, schema, table, _ in self.create_tables
            if schema != "pilot"
        ]
        self.assertFalse(
            offenders,
            f"CREATE TABLE must be pilot-qualified (not unqualified or public.*): {offenders}",
        )

    def test_known_extras_exist_in_migrations(self) -> None:
        missing = sorted(KNOWN_SQL_ONLY_TABLES - set(self.sql_tables))
        self.assertEqual(missing, [], f"known extra migration tables not found: {missing}")

    def test_bigint_columns_match_orm_width(self) -> None:
        """Real Postgres rejected a 63-bit seed mapped as 32-bit INTEGER.

        SQLite hides integer width, so pin BIGINT columns against the ORM.
        """
        from sqlalchemy import BigInteger

        sql = PILOT_SCHEMA_FILE.read_text(encoding="utf-8")
        sql_bigint = {
            match.group(1)
            for match in re.finditer(r"^\s*([A-Za-z_][\w]*)\s+BIGINT\b", sql, re.MULTILINE)
        }
        model_bigint = {
            column.name
            for table in Base.metadata.tables.values()
            for column in table.columns
            if isinstance(column.type, BigInteger)
        }
        self.assertEqual(
            sql_bigint,
            model_bigint,
            f"BIGINT drift: migration={sql_bigint}, models={model_bigint}",
        )
        self.assertIn("randomization_seed", model_bigint)


if __name__ == "__main__":
    unittest.main()
