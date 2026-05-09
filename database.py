import unicodedata
import os

from indic_transliteration import sanscript
from sqlalchemy import create_engine, Column, Date, Enum, Integer, String, ForeignKey, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

GENDER_MALE = "MALE"
GENDER_FEMALE = "FEMALE"
GENDER_VALUES = (GENDER_MALE, GENDER_FEMALE)

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

def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        credential = db.query(AdminCredential).first()
        if not credential:
            db.add(AdminCredential(password="admusr"))
            db.commit()
    finally:
        db.close()

def to_english(kannada_text):
    if not kannada_text:
        return None

    transliterated = sanscript.transliterate(kannada_text, sanscript.KANNADA, sanscript.IAST)
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
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
