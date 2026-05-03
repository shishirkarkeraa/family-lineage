import os

from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from database import engine, get_db, migrate_person_name_columns, SessionLocal, Person, AdminCredential

app = FastAPI(title="Karkera Family")

@app.on_event("startup")
def startup():
    migrate_person_name_columns()

class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        password = form.get("password")

        db = SessionLocal()
        try:
            credential = db.query(AdminCredential).first()
            if credential and password == credential.password:
                request.session.update({"admin_logged_in": True})
                return True
        finally:
            db.close()

        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return bool(request.session.get("admin_logged_in"))

admin = Admin(
    app,
    engine,
    authentication_backend=AdminAuth(
        secret_key=os.getenv("ADMIN_SESSION_SECRET", "family-lineage-admin")
    ),
)

class PersonAdmin(ModelView, model=Person):
    column_list = [Person.id, Person.name_en, Person.name_kn, Person.gender, Person.parent_id]
    form_columns = [Person.name_en, Person.name_kn, Person.gender, Person.parent_id]
    column_searchable_list = [Person.name_en, Person.name_kn]
    column_sortable_list = [Person.name_en, Person.name_kn, Person.id]
    column_labels = {
        Person.name_en: "English Name",
        Person.name_kn: "Kannada Name",
    }

admin.add_view(PersonAdmin)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("base.html", {"request": request})

@app.get("/tree", response_class=HTMLResponse)
async def tree_view(request: Request, db: Session = Depends(get_db)):
    persons = db.query(Person).order_by(Person.name_kn).all()
    levels = get_generation_levels(persons)
    nodes = [
        {
            "id": p.id,
            "name_en": p.name_en,
            "name_kn": p.name_kn,
            "gender": p.gender,
            "level": levels.get(p.id, 0),
        }
        for p in persons
    ]
    edges = [{"from": p.parent_id, "to": p.id} for p in persons if p.parent_id]
    return templates.TemplateResponse("tree.html", {"request": request, "nodes": nodes, "edges": edges})

@app.get("/lineage", response_class=HTMLResponse)
async def lineage_view(request: Request, db: Session = Depends(get_db)):
    persons = db.query(Person).order_by(Person.name_kn).all()
    return templates.TemplateResponse("lineage.html", {"request": request, "persons": persons})

@app.get("/api/lineage/{person_id}")
async def get_lineage(person_id: int, db: Session = Depends(get_db)):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        return {"error": "Person not found"}

    path = []
    current = person
    while current:
        path.append(person_payload(current, selected=current.id == person.id))
        current = db.query(Person).filter(Person.id == current.parent_id).first() if current.parent_id else None

    path.reverse()
    return {"selected_id": person.id, "path": path}

@app.get("/relationship", response_class=HTMLResponse)
async def relationship_view(request: Request, db: Session = Depends(get_db)):
    persons = db.query(Person).order_by(Person.name_kn).all()
    return templates.TemplateResponse("relationship.html", {"request": request, "persons": persons})

@app.get("/api/relationship")
async def get_relationship(p1_id: int, p2_id: int, db: Session = Depends(get_db)):
    person1 = db.query(Person).filter(Person.id == p1_id).first()
    person2 = db.query(Person).filter(Person.id == p2_id).first()

    if not person1 or not person2:
        return {
            "relationship_en": "Person not found.",
            "relationship_kn": "ವ್ಯಕ್ತಿ ಕಂಡುಬಂದಿಲ್ಲ.",
        }

    relation_en, relation_kn = describe_relationship(person1, person2, db)

    if relation_en == "same person":
        relationship_en = f"{person1.name_en} and {person2.name_en} are the same person."
        relationship_kn = f"{person1.name_kn} ಮತ್ತು {person2.name_kn} ಅದೇ ವ್ಯಕ್ತಿ."
    elif relation_en == "not directly related":
        relationship_en = f"{person1.name_en} and {person2.name_en} are not directly related."
        relationship_kn = f"{person1.name_kn} ಮತ್ತು {person2.name_kn} ಅವರಿಗೆ ನೇರ ಸಂಬಂಧ ಇಲ್ಲ."
    else:
        relationship_en = f"{person1.name_en} is {person2.name_en}'s {relation_en}."
        relationship_kn = f"{person1.name_kn} ಅವರು {person2.name_kn} ಅವರ {relation_kn}."

    return {
        "relationship_en": relationship_en,
        "relationship_kn": relationship_kn,
        "relation_en": relation_en,
        "relation_kn": relation_kn,
    }

def person_payload(person, selected=False):
    return {
        "id": person.id,
        "name_en": person.name_en,
        "name_kn": person.name_kn,
        "gender": person.gender,
        "selected": selected,
    }

def get_generation_levels(persons):
    by_id = {person.id: person for person in persons}
    levels = {}

    def level_for(person):
        if person.id in levels:
            return levels[person.id]
        if not person.parent_id or person.parent_id not in by_id:
            levels[person.id] = 0
        else:
            levels[person.id] = level_for(by_id[person.parent_id]) + 1
        return levels[person.id]

    for person in persons:
        level_for(person)

    return levels

def ancestor_distances(person, db):
    distances = {}
    distance = 0
    current = person

    while current:
        distances[current.id] = distance
        current = db.query(Person).filter(Person.id == current.parent_id).first() if current.parent_id else None
        distance += 1

    return distances

def gendered(person, male, female, neutral):
    if person.gender == "Male":
        return male
    if person.gender == "Female":
        return female
    return neutral

def ancestor_relation(person, distance):
    if distance == 1:
        return gendered(person, "father", "mother", "parent"), gendered(person, "ತಂದೆ", "ತಾಯಿ", "ಪೋಷಕರು")
    if distance == 2:
        return gendered(person, "grandfather", "grandmother", "grandparent"), gendered(person, "ಅಜ್ಜ", "ಅಜ್ಜಿ", "ಅಜ್ಜ/ಅಜ್ಜಿ")

    prefix = "great-" * (distance - 2)
    return (
        gendered(person, f"{prefix}grandfather", f"{prefix}grandmother", f"{prefix}grandparent"),
        gendered(person, "ಮುತ್ತಜ್ಜ", "ಮುತ್ತಜ್ಜಿ", "ಹಿರಿಯ ಪೂರ್ವಜರು"),
    )

def descendant_relation(person, distance):
    if distance == 1:
        return gendered(person, "son", "daughter", "child"), gendered(person, "ಮಗ", "ಮಗಳು", "ಮಗು")
    if distance == 2:
        return gendered(person, "grandson", "granddaughter", "grandchild"), gendered(person, "ಮೊಮ್ಮಗ", "ಮೊಮ್ಮಗಳು", "ಮೊಮ್ಮಗು")

    prefix = "great-" * (distance - 2)
    return (
        gendered(person, f"{prefix}grandson", f"{prefix}granddaughter", f"{prefix}grandchild"),
        gendered(person, "ಮರಿಮೊಮ್ಮಗ", "ಮರಿಮೊಮ್ಮಗಳು", "ಮುಂದಿನ ವಂಶಜರು"),
    )

def ordinal(value):
    names = {
        1: "first",
        2: "second",
        3: "third",
        4: "fourth",
        5: "fifth",
        6: "sixth",
        7: "seventh",
        8: "eighth",
        9: "ninth",
        10: "tenth",
    }
    return names.get(value, f"{value}th")

def removal_text(value):
    if value == 0:
        return ""
    if value == 1:
        return " once removed"
    if value == 2:
        return " twice removed"
    return f" {value} times removed"

def extended_relation(person, distance1, distance2):
    if distance1 == 1 and distance2 > 2:
        prefix = "great-" * (distance2 - 2)
        return (
            gendered(person, f"{prefix}uncle", f"{prefix}aunt", f"{prefix}aunt/uncle"),
            gendered(person, "ಹಿರಿಯ ಮಾಮ/ಚಿಕ್ಕಪ್ಪ", "ಹಿರಿಯ ಅತ್ತೆ/ಚಿಕ್ಕಮ್ಮ", "ಹಿರಿಯ ಅತ್ತೆ/ಮಾಮ"),
        )

    if distance1 > 2 and distance2 == 1:
        prefix = "great-" * (distance1 - 2)
        return (
            gendered(person, f"{prefix}nephew", f"{prefix}niece", f"{prefix}niece/nephew"),
            gendered(person, "ಮುಂದಿನ ತಲೆಮಾರಿನ ಸೋದರಳಿಯ", "ಮುಂದಿನ ತಲೆಮಾರಿನ ಸೋದರ ಸೊಸೆ", "ಮುಂದಿನ ತಲೆಮಾರಿನ ಸೋದರ ಮಗು"),
        )

    degree = min(distance1, distance2) - 1
    removed = abs(distance1 - distance2)
    return (
        f"{ordinal(degree)} cousin{removal_text(removed)}",
        f"{degree}ನೇ ಹಂತದ ಸೋದರ ಸಂಬಂಧಿ" + (f", {removed} ತಲೆಮಾರಿನ ಅಂತರ" if removed else ""),
    )

def describe_relationship(person1, person2, db):
    if person1.id == person2.id:
        return "same person", "ಅದೇ ವ್ಯಕ್ತಿ"

    ancestors1 = ancestor_distances(person1, db)
    ancestors2 = ancestor_distances(person2, db)

    if person1.id in ancestors2:
        return ancestor_relation(person1, ancestors2[person1.id])

    if person2.id in ancestors1:
        return descendant_relation(person1, ancestors1[person2.id])

    common_ids = set(ancestors1).intersection(ancestors2)
    if not common_ids:
        return "not directly related", "ನೇರ ಸಂಬಂಧ ಇಲ್ಲ"

    lca_id = min(common_ids, key=lambda person_id: ancestors1[person_id] + ancestors2[person_id])
    distance1 = ancestors1[lca_id]
    distance2 = ancestors2[lca_id]

    if distance1 == 1 and distance2 == 1:
        return gendered(person1, "brother", "sister", "sibling"), gendered(person1, "ಸಹೋದರ", "ಸಹೋದರಿ", "ಸಹೋದರ/ಸಹೋದರಿ")

    if distance1 == 1 and distance2 == 2:
        return gendered(person1, "uncle", "aunt", "aunt/uncle"), gendered(person1, "ಮಾಮ/ಚಿಕ್ಕಪ್ಪ", "ಅತ್ತೆ/ಚಿಕ್ಕಮ್ಮ", "ಅತ್ತೆ/ಮಾಮ")

    if distance1 == 2 and distance2 == 1:
        return gendered(person1, "nephew", "niece", "niece/nephew"), gendered(person1, "ಸೋದರಳಿಯ", "ಸೋದರ ಸೊಸೆ", "ಸೋದರ ಮಗು")

    if distance1 == 2 and distance2 == 2:
        return "cousin", "ಸೋದರ ಸಂಬಂಧಿ"

    return extended_relation(person1, distance1, distance2)
