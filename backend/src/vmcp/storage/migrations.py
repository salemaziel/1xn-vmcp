"""
Database migration system for vMCP OSS version.

Handles schema changes while preserving existing data.
"""

import logging
import secrets
from typing import List, Dict, Any
from sqlalchemy import text, inspect
from sqlalchemy.exc import OperationalError

from vmcp.storage.database import get_engine
from vmcp.config import settings
from vmcp.utilities.logging import get_logger

logger = get_logger(__name__)


class DatabaseMigrator:
    """Handles database schema migrations."""
    
    def __init__(self):
        self.engine = get_engine()
        self.inspector = inspect(self.engine)
    
    def get_migration_version(self) -> int:
        """Get the current migration version from the database."""
        try:
            with self.engine.connect() as conn:
                # Check if migrations table exists
                if 'migrations' not in self.inspector.get_table_names():
                    return 0
                
                result = conn.execute(text("SELECT version FROM migrations ORDER BY version DESC LIMIT 1"))
                row = result.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.warning(f"Could not get migration version: {e}")
            return 0
    
    def set_migration_version(self, version: int) -> None:
        """Set the migration version in the database."""
        try:
            with self.engine.connect() as conn:
                # Create migrations table if it doesn't exist
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        description TEXT
                    )
                """))
                conn.commit()

                # Insert or update version - use database-specific syntax
                is_postgres = "postgresql" in settings.database_url

                if is_postgres:
                    # PostgreSQL: Use ON CONFLICT
                    conn.execute(text("""
                        INSERT INTO migrations (version, description)
                        VALUES (:version, :description)
                        ON CONFLICT (version) DO UPDATE
                        SET description = EXCLUDED.description
                    """), {"version": version, "description": f"Migration {version}"})
                else:
                    # SQLite: Use INSERT OR REPLACE
                    conn.execute(text("""
                        INSERT OR REPLACE INTO migrations (version, description)
                        VALUES (:version, :description)
                    """), {"version": version, "description": f"Migration {version}"})

                conn.commit()

                logger.info(f"Set migration version to {version}")
        except Exception as e:
            logger.error(f"Failed to set migration version: {e}")
            raise
    
    def run_migrations(self) -> None:
        """Run all pending migrations."""
        current_version = self.get_migration_version()
        logger.info(f"Current migration version: {current_version}")
        
        # Define migrations
        migrations = [
            (1, self._migration_001_add_blob_columns),
            (2, self._migration_002_fix_widget_id_constraint),
            (3, self._migration_003_add_user_auth_columns),
            (4, self._migration_004_add_oauth_tables),
        ]
        
        # Run pending migrations
        for version, migration_func in migrations:
            if version > current_version:
                logger.info(f"Running migration {version}...")
                try:
                    migration_func()
                    self.set_migration_version(version)
                    logger.info(f"Migration {version} completed successfully")
                except Exception as e:
                    logger.error(f"Migration {version} failed: {e}")
                    raise
        
        logger.info("All migrations completed")
    
    def _migration_001_add_blob_columns(self) -> None:
        """Add missing columns to blobs table for vMCP support."""
        try:
            with self.engine.connect() as conn:
                # Check if blobs table exists
                if 'blobs' not in self.inspector.get_table_names():
                    logger.info("blobs table does not exist, skipping migration")
                    return
                
                # Get existing columns
                existing_columns = [col['name'] for col in self.inspector.get_columns('blobs')]
                logger.info(f"Existing blobs columns: {existing_columns}")
                
                # First, make widget_id nullable if it exists and is NOT NULL
                if 'widget_id' in existing_columns:
                    try:
                        # Check if widget_id is currently NOT NULL
                        columns_info = self.inspector.get_columns('blobs')
                        widget_id_col = next((col for col in columns_info if col['name'] == 'widget_id'), None)
                        if widget_id_col and not widget_id_col.get('nullable', True):
                            logger.info("Making widget_id column nullable")
                            # SQLite doesn't support ALTER COLUMN, so we need to recreate the table
                            # But for now, let's just add the new columns and handle the constraint issue
                            pass
                    except Exception as e:
                        logger.warning(f"Could not check widget_id constraint: {e}")
                
                # Add missing columns
                columns_to_add = [
                    ("vmcp_id", "VARCHAR", "NULL"),
                    ("original_filename", "VARCHAR(500)", "NOT NULL DEFAULT ''"),
                    ("filename", "VARCHAR(500)", "NOT NULL DEFAULT ''"),
                    ("resource_name", "VARCHAR(500)", "NULL"),
                    ("is_public", "BOOLEAN", "NOT NULL DEFAULT 0"),
                ]
                
                for column_name, column_type, constraints in columns_to_add:
                    if column_name not in existing_columns:
                        logger.info(f"Adding column {column_name} to blobs table")
                        sql = f"ALTER TABLE blobs ADD COLUMN {column_name} {column_type} {constraints}"
                        conn.execute(text(sql))
                    else:
                        logger.info(f"Column {column_name} already exists, skipping")
                
                # Create indexes for new columns
                indexes_to_add = [
                    ("idx_blob_vmcp", "vmcp_id"),
                ]
                
                for index_name, column_name in indexes_to_add:
                    try:
                        # Check if index already exists
                        existing_indexes = [idx['name'] for idx in self.inspector.get_indexes('blobs')]
                        if index_name not in existing_indexes and column_name in existing_columns:
                            logger.info(f"Creating index {index_name} on {column_name}")
                            sql = f"CREATE INDEX {index_name} ON blobs ({column_name})"
                            conn.execute(text(sql))
                    except Exception as e:
                        logger.warning(f"Could not create index {index_name}: {e}")
                
                conn.commit()
                logger.info("Migration 001 completed: Added blob columns for vMCP support")
                
        except Exception as e:
            logger.error(f"Migration 001 failed: {e}")
            raise
    
    def _migration_002_fix_widget_id_constraint(self) -> None:
        """Fix widget_id NOT NULL constraint by recreating the blobs table."""
        try:
            with self.engine.connect() as conn:
                # Check if blobs table exists
                if 'blobs' not in self.inspector.get_table_names():
                    logger.info("blobs table does not exist, skipping migration")
                    return
                
                logger.info("Fixing widget_id constraint by recreating blobs table...")
                
                # Get existing data
                result = conn.execute(text("SELECT * FROM blobs"))
                existing_data = result.fetchall()
                # Get column names from the table schema instead
                columns_info = self.inspector.get_columns('blobs')
                column_names = [col['name'] for col in columns_info]
                
                logger.info(f"Found {len(existing_data)} existing blob records")
                
                # Create new table with correct schema
                # Use database-specific datetime type
                is_postgres = "postgresql" in settings.database_url
                datetime_type = "TIMESTAMP" if is_postgres else "DATETIME"
                boolean_default = "FALSE" if is_postgres else "0"

                conn.execute(text(f"""
                    CREATE TABLE blobs_new (
                        id VARCHAR PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        widget_id VARCHAR NULL,
                        vmcp_id VARCHAR NULL,
                        original_filename VARCHAR(500) NOT NULL DEFAULT '',
                        filename VARCHAR(500) NOT NULL DEFAULT '',
                        file_path VARCHAR NULL,
                        resource_name VARCHAR(500) NULL,
                        content TEXT NOT NULL,
                        content_type VARCHAR(255) NOT NULL,
                        size INTEGER NOT NULL,
                        checksum VARCHAR NULL,
                        is_public BOOLEAN NOT NULL DEFAULT {boolean_default},
                        created_at {datetime_type} NULL,
                        updated_at {datetime_type} NULL
                    )
                """))
                
                # Copy data from old table to new table
                if existing_data:
                    for row in existing_data:
                        # Create a dict from the row data
                        row_dict = dict(zip(column_names, row))
                        
                        # Insert into new table with proper NULL handling
                        conn.execute(text("""
                            INSERT INTO blobs_new (
                                id, user_id, widget_id, vmcp_id, original_filename, 
                                filename, file_path, resource_name, content, content_type, 
                                size, checksum, is_public, created_at, updated_at
                            ) VALUES (
                                :id, :user_id, :widget_id, :vmcp_id, :original_filename,
                                :filename, :file_path, :resource_name, :content, :content_type,
                                :size, :checksum, :is_public, :created_at, :updated_at
                            )
                        """), row_dict)
                
                # Drop old table and rename new table
                conn.execute(text("DROP TABLE blobs"))
                conn.execute(text("ALTER TABLE blobs_new RENAME TO blobs"))
                
                # Create indexes
                conn.execute(text("CREATE INDEX idx_blob_widget ON blobs (widget_id)"))
                conn.execute(text("CREATE INDEX idx_blob_user ON blobs (user_id)"))
                conn.execute(text("CREATE INDEX idx_blob_vmcp ON blobs (vmcp_id)"))
                
                conn.commit()
                logger.info("Migration 002 completed: Fixed widget_id constraint")
                
        except Exception as e:
            logger.error(f"Migration 002 failed: {e}")
            raise

    def _migration_003_add_user_auth_columns(self) -> None:
        """Add authentication-related columns to the users table."""
        try:
            with self.engine.connect() as conn:
                if 'users' not in self.inspector.get_table_names():
                    logger.info("users table does not exist, skipping migration")
                    return

                existing_columns = [col['name'] for col in self.inspector.get_columns('users')]
                is_postgres = "postgresql" in settings.database_url
                boolean_type = "BOOLEAN"
                true_value = "TRUE" if is_postgres else "1"

                columns_to_add = [
                    ("password_hash", "VARCHAR(512)", "NULL"),
                    ("is_active", boolean_type, f"NOT NULL DEFAULT {true_value}"),
                    ("is_verified", boolean_type, f"NOT NULL DEFAULT {true_value}"),
                    ("session_nonce", "VARCHAR(64)", "NOT NULL DEFAULT ''"),
                    ("last_login", "TIMESTAMP" if is_postgres else "DATETIME", "NULL"),
                ]

                for column_name, column_type, constraints in columns_to_add:
                    if column_name not in existing_columns:
                        logger.info(f"Adding column {column_name} to users table")
                        conn.execute(text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type} {constraints}"))

                empty_nonce_sql = "session_nonce IS NULL OR session_nonce = ''"
                result = conn.execute(text(f"SELECT id FROM users WHERE {empty_nonce_sql}"))
                updates = [
                    {"session_nonce": secrets.token_hex(16), "user_id": row[0]}
                    for row in result.fetchall()
                ]
                if updates:
                    conn.execute(
                        text("UPDATE users SET session_nonce = :session_nonce WHERE id = :user_id"),
                        updates,
                    )

                conn.commit()
                logger.info("Migration 003 completed: Added user authentication columns")
        except Exception as e:
            logger.error(f"Migration 003 failed: {e}")
            raise

    def _migration_004_add_oauth_tables(self) -> None:
        """Add OAuth account link and login state tables."""
        try:
            with self.engine.connect() as conn:
                is_postgres = "postgresql" in settings.database_url
                boolean_default = "FALSE" if is_postgres else "0"
                datetime_type = "TIMESTAMP" if is_postgres else "DATETIME"
                integer_pk = "SERIAL PRIMARY KEY" if is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS user_oauth_accounts (
                            id {integer_pk},
                            user_id INTEGER NOT NULL,
                            provider VARCHAR(50) NOT NULL,
                            issuer VARCHAR(255) NOT NULL,
                            subject VARCHAR(255) NOT NULL,
                            email VARCHAR(255) NULL,
                            email_verified BOOLEAN NOT NULL DEFAULT {boolean_default},
                            picture_url VARCHAR(1024) NULL,
                            claims_json TEXT NULL,
                            last_login_at {datetime_type} NULL,
                            created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP NOT NULL,
                            updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP NOT NULL,
                            FOREIGN KEY(user_id) REFERENCES users(id)
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_oauth_account_unique_identity
                        ON user_oauth_accounts (provider, issuer, subject)
                        """
                    )
                )
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS idx_user_oauth_accounts_user_id ON user_oauth_accounts (user_id)")
                )
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS idx_user_oauth_accounts_email ON user_oauth_accounts (email)")
                )

                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS oauth_login_states (
                            id {integer_pk},
                            provider VARCHAR(50) NOT NULL,
                            state_hash VARCHAR(64) NOT NULL,
                            code_verifier VARCHAR(255) NOT NULL,
                            nonce VARCHAR(255) NOT NULL,
                            requested_username VARCHAR(50) NULL,
                            return_to VARCHAR(1024) NOT NULL,
                            created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP NOT NULL,
                            expires_at {datetime_type} NOT NULL,
                            used_at {datetime_type} NULL
                        )
                        """
                    )
                )
                conn.execute(
                    text("CREATE UNIQUE INDEX IF NOT EXISTS idx_oauth_login_states_state_hash ON oauth_login_states (state_hash)")
                )
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS idx_oauth_login_states_expires_at ON oauth_login_states (expires_at)")
                )

                conn.commit()
                logger.info("Migration 004 completed: Added OAuth account and login state tables")
        except Exception as e:
            logger.error(f"Migration 004 failed: {e}")
            raise


def run_migrations() -> None:
    """Run all pending database migrations."""
    migrator = DatabaseMigrator()
    migrator.run_migrations()
