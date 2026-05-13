import unicodedata
import os
from datetime import datetime

from sqlalchemy import create_engine, Column, Date, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

try:
    from indic_transliteration import sanscript
except ModuleNotFoundError:
    sanscript = None

GENDER_MALE = "MALE"
GENDER_FEMALE = "FEMALE"
GENDER_VALUES = (GENDER_MALE, GENDER_FEMALE)
CHANGE_ADD_DESCENDANT = "ADD_DESCENDANT"
CHANGE_EDIT_PERSON = "EDIT_PERSON"
CHANGE_TYPES = (CHANGE_ADD_DESCENDANT, CHANGE_EDIT_PERSON)
CHANGE_PENDING = "PENDING"
CHANGE_APPROVED = "APPROVED"
CHANGE_REJECTED = "REJECTED"
CHANGE_STATUSES = (CHANGE_PENDING, CHANGE_APPROVED, CHANGE_REJECTED)

def normalize_gender(value):
    if not value:
        return None

    normalized = value.strip().upper()
    if normalized not in GENDER_VALUES:
        raise ValueError("Gender must be MALE or FEMALE.")
    return normalized

def load_env_file(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

load_env_file()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"connect_timeout": 10},
    pool_pre_ping=True,
    pool_recycle=300,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Person(Base):
    __tablename__ = "persons"

    id = Column(Integer, primary_key=True, index=True)
    name_en = Column(String, index=True)
    name_kn = Column(String, index=True)
    gender = Column(Enum(*GENDER_VALUES, name="gender_enum", validate_strings=True), nullable=True)
    parent_id = Column(Integer, ForeignKey("persons.id"), nullable=True)
    parent2_id = Column(Integer, ForeignKey("persons.id"), nullable=True)
    date_of_birth = Column(Date, nullable=True)

    children = relationship(
        "Person",
        backref="parent",
        remote_side=[id],
        foreign_keys=[parent_id],
    )

class AdminCredential(Base):
    __tablename__ = "admin_credentials"

    id = Column(Integer, primary_key=True, index=True)
    password = Column(String, nullable=False)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    google_sub = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    name = Column(String, nullable=True)
    picture = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class ChangeRequest(Base):
    __tablename__ = "changes"

    id = Column(Integer, primary_key=True, index=True)
    change_type = Column(String, nullable=False, index=True)
    status = Column(String, default=CHANGE_PENDING, nullable=False, index=True)
    target_person_id = Column(Integer, ForeignKey("persons.id"), nullable=True)
    parent_person_id = Column(Integer, ForeignKey("persons.id"), nullable=True)
    payload = Column(JSON, nullable=False)
    requester_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    requester_email = Column(String, nullable=True)
    requester_name = Column(String, nullable=True)
    reviewer_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)

    requester = relationship("User")
    target_person = relationship("Person", foreign_keys=[target_person_id])
    parent_person = relationship("Person", foreign_keys=[parent_person_id])

_base_tables_ready = False

def ensure_base_tables():
    global _base_tables_ready
    if _base_tables_ready:
        return

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        credential = db.query(AdminCredential).first()
        if not credential:
            db.add(AdminCredential(password="admusr"))
            db.commit()
    finally:
        db.close()

    _base_tables_ready = True

def init_db():
    ensure_base_tables()

def to_english(kannada_text):
    if not kannada_text:
        return None

    transliterated = (
        sanscript.transliterate(kannada_text, sanscript.KANNADA, sanscript.IAST)
        if sanscript
        else kannada_text
    )
    ascii_name = ''.join(
        char
        for char in unicodedata.normalize('NFD', transliterated)
        if unicodedata.category(char) != 'Mn'
    )
    return ' '.join(word.capitalize() for word in ascii_name.split())

def migrate_person_name_columns():
    inspector = inspect(engine)
    if not inspector.has_table("persons"):
        init_db()
        return

    column_info = {column["name"]: column for column in inspector.get_columns("persons")}
    columns = set(column_info)

    with engine.begin() as connection:
        if "name_kn" not in columns:
            connection.execute(text("ALTER TABLE persons ADD COLUMN name_kn VARCHAR"))
        if "name_en" not in columns:
            connection.execute(text("ALTER TABLE persons ADD COLUMN name_en VARCHAR"))
        if "parent2_id" not in columns:
            connection.execute(text("ALTER TABLE persons ADD COLUMN parent2_id INTEGER REFERENCES persons(id)"))
        if "date_of_birth" not in columns:
            connection.execute(text("ALTER TABLE persons ADD COLUMN date_of_birth DATE"))
        if "gender" not in columns:
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text(
                        "DO $$ BEGIN "
                        "CREATE TYPE gender_enum AS ENUM ('MALE', 'FEMALE'); "
                        "EXCEPTION WHEN duplicate_object THEN NULL; "
                        "END $$"
                    )
                )
                connection.execute(text("ALTER TABLE persons ADD COLUMN gender gender_enum"))
            else:
                connection.execute(text("ALTER TABLE persons ADD COLUMN gender VARCHAR"))

        if engine.dialect.name == "postgresql":
            connection.execute(text("UPDATE persons SET gender = 'MALE' WHERE lower(gender::text) = 'male'"))
            connection.execute(text("UPDATE persons SET gender = 'FEMALE' WHERE lower(gender::text) = 'female'"))
            connection.execute(text("UPDATE persons SET gender = NULL WHERE gender::text NOT IN ('MALE', 'FEMALE')"))
        else:
            connection.execute(text("UPDATE persons SET gender = 'MALE' WHERE lower(gender) = 'male'"))
            connection.execute(text("UPDATE persons SET gender = 'FEMALE' WHERE lower(gender) = 'female'"))
            connection.execute(text("UPDATE persons SET gender = NULL WHERE gender NOT IN ('MALE', 'FEMALE')"))

        gender_column = column_info.get("gender")
        if (
            gender_column
            and engine.dialect.name == "postgresql"
            and gender_column["type"].__class__.__name__ != "ENUM"
        ):
            connection.execute(
                text(
                    "DO $$ BEGIN "
                    "CREATE TYPE gender_enum AS ENUM ('MALE', 'FEMALE'); "
                    "EXCEPTION WHEN duplicate_object THEN NULL; "
                    "END $$"
                )
            )
            connection.execute(
                text("ALTER TABLE persons ALTER COLUMN gender TYPE gender_enum USING gender::gender_enum")
            )

        if "name" in columns:
            rows = connection.execute(
                text(
                    "SELECT id, name, name_kn, name_en FROM persons "
                    "WHERE name IS NOT NULL AND (name_kn IS NULL OR name_en IS NULL)"
                )
            ).mappings()

            for row in rows:
                name_kn = row["name_kn"] or row["name"]
                name_en = row["name_en"] or to_english(name_kn)
                connection.execute(
                    text("UPDATE persons SET name_kn = :name_kn, name_en = :name_en WHERE id = :id"),
                    {"id": row["id"], "name_kn": name_kn, "name_en": name_en},
                )

    init_db()

def get_db():
    ensure_base_tables()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
