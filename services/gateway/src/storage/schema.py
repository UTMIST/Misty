"""SQLAlchemy Core Table definitions for the gateway's api_keys store.

This module defines TABLES, not ORM classes. All queries in
PostgresStorageAdapter use core-style expressions against these tables.
"""

from sqlalchemy import Boolean, Column, DateTime, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID

metadata = MetaData()

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("name", Text, nullable=False, unique=True),
    Column("prefix", Text, nullable=False, unique=True),
    Column("key_hash", Text, nullable=False),
    Column("scopes", ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("created_by", Text, nullable=False),
    Column("updated_by", Text, nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
)
