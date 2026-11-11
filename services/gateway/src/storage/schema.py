"""SQLAlchemy Core Table definitions for the gateway's api_keys store.

This module defines TABLES, not ORM classes. All queries in
PostgresStorageAdapter use core-style expressions against these tables.
"""

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, MetaData, Table, Text, text
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

login_challenges = Table(
    "login_challenges",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("person_id", UUID(as_uuid=True), nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("redeemed", Boolean, nullable=False, server_default=text("false")),
    Index("ix_login_challenges_owner_email", "owner", "email"),
)

login_limits = Table(
    "login_limits",
    metadata,
    Column("bucket", Text, primary_key=True),
    Column("count", Integer, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
)
