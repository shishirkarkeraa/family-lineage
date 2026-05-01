import unicodedata
import os

from indic_transliteration import sanscript
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

engine = create_engine(DATABASE_URL, connect_args={"connect_timeout": 10})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Person(Base):
    __tablename__ = "persons"

    id = Column(Integer, primary_key=True, index=True)
    name_en = Column(String, index=True)
    name_kn = Column(String, index=True)
    gender = Column(String, nullable=True)
    parent_id = Column(Integer, ForeignKey("persons.id"), nullable=True)

    children = relationship("Person", backref="parent", remote_side=[id])

def init_db():
    Base.metadata.create_all(bind=engine)

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

    columns = {column["name"] for column in inspector.get_columns("persons")}

    with engine.begin() as connection:
        if "name_kn" not in columns:
            connection.execute(text("ALTER TABLE persons ADD COLUMN name_kn VARCHAR"))
        if "name_en" not in columns:
            connection.execute(text("ALTER TABLE persons ADD COLUMN name_en VARCHAR"))

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
