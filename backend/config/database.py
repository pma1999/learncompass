from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv
from urllib.parse import quote_plus
import sqlite3
from datetime import datetime

# Define the path to the .env file relative to this script
dotenv_path = os.path.join(os.path.dirname(__file__), '../.env')

# Load environment variables from the specified path
load_dotenv(dotenv_path=dotenv_path)

# Get database connection details from environment variables
DATABASE_URL = os.getenv("DATABASE_URL")

# If DATABASE_URL is not set, create a default SQLite database for development
if not DATABASE_URL:
    # Default to SQLite for easier development
    sqlite_db_path = os.path.join(os.getcwd(), "learni.db")
    DATABASE_URL = f"sqlite:///{sqlite_db_path}"
    print(f"Using SQLite database at: {sqlite_db_path}")
elif DATABASE_URL.startswith("postgres"):
    # Handle PostgreSQL URLs with potential password encoding issues
    try:
        # If there are special characters in the password, they need to be encoded
        if "@" in DATABASE_URL:
            prefix, rest = DATABASE_URL.split("://")
            auth, server = rest.split("@")
            if ":" in auth:
                user, password = auth.split(":")
                # URL encode the password to handle special characters
                password = quote_plus(password)
                DATABASE_URL = f"{prefix}://{user}:{password}@{server}"
    except Exception as e:
        print(f"Warning: Error processing DATABASE_URL: {e}")
        # Fall back to SQLite if there's an issue
        sqlite_db_path = os.path.join(os.getcwd(), "learni.db")
        DATABASE_URL = f"sqlite:///{sqlite_db_path}"
        print(f"Falling back to SQLite database at: {sqlite_db_path}")

# Create SQLAlchemy engine with appropriate settings
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    # SQLite-specific settings: enable foreign key constraints
    connect_args = {"check_same_thread": False}
    engine = create_engine(DATABASE_URL, connect_args=connect_args, echo=False)
else:
    # PostgreSQL or other database engines
    engine = create_engine(DATABASE_URL, pool_size=5, max_overflow=10, echo=False)

print(f"Using database: {DATABASE_URL}")

# Create SessionLocal class for creating database sessions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create a base class for declarative models
Base = declarative_base()

def apply_migrations():
    """Apply any necessary database migrations during startup."""
    import sqlalchemy
    from sqlalchemy.sql import text
    
    # Check database type from connection URL
    if DATABASE_URL.startswith("sqlite:///"):
        # SQLite migration
        db_path = DATABASE_URL.replace("sqlite:///", "")
        
        # Connect to SQLite database
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        try:
            # Check if sessions table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
            if cursor.fetchone():
                # Check if the column exists
                cursor.execute("PRAGMA table_info(sessions)")
                columns = cursor.fetchall()
                column_names = [col[1] for col in columns]
                
                # Add last_used_at column if it doesn't exist
                if "last_used_at" not in column_names:
                    print("Adding last_used_at column to sessions table (SQLite)...")
                    cursor.execute("ALTER TABLE sessions ADD COLUMN last_used_at TIMESTAMP")
                    
                    # Set default values for existing records
                    current_time = datetime.utcnow().isoformat()
                    cursor.execute(f"UPDATE sessions SET last_used_at = '{current_time}'")
                    
                    # Create index on user_id if it doesn't exist
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_user_id ON sessions (user_id)")
                    
                    print("Migration completed successfully (SQLite)")
                else:
                    print("Column last_used_at already exists (SQLite)")
            
            # Commit changes
            conn.commit()
            
        except Exception as e:
            print(f"Error during SQLite migration: {e}")
            conn.rollback()
        finally:
            conn.close()
            
    elif DATABASE_URL.startswith("postgresql://") or "postgres" in DATABASE_URL:
        # PostgreSQL migration using SQLAlchemy
        try:
            # Create a connection to use with raw SQL
            with engine.begin() as connection:
                # Check if the table exists
                result = connection.execute(text("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'sessions')"))
                table_exists = result.scalar()
                
                if table_exists:
                    # Check if the column exists
                    result = connection.execute(text("SELECT EXISTS (SELECT FROM information_schema.columns WHERE table_name = 'sessions' AND column_name = 'last_used_at')"))
                    column_exists = result.scalar()
                    
                    if not column_exists:
                        print("Adding last_used_at column to sessions table (PostgreSQL)...")
                        
                        # Add the column
                        connection.execute(text("ALTER TABLE sessions ADD COLUMN last_used_at TIMESTAMP"))
                        
                        # Set default values for existing records
                        connection.execute(text("UPDATE sessions SET last_used_at = NOW()"))
                        
                        # Create index on user_id if it doesn't exist
                        connection.execute(text(
                            "CREATE INDEX IF NOT EXISTS idx_session_user_id ON sessions (user_id)"
                        ))
                        
                        print("Migration completed successfully (PostgreSQL)")
                    else:
                        print("Column last_used_at already exists (PostgreSQL)")
                        
        except Exception as e:
            print(f"Error during PostgreSQL migration: {e}")
            raise
    
    else:
        print(f"Unsupported database type for automatic migrations: {DATABASE_URL}")
        print("Manual database migration may be required")

# Dependency to get the database session
def get_db():
    """
    Dependency function to get a database session.
    Used in FastAPI route dependencies.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close() 