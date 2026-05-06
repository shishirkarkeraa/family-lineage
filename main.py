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
async def read_root(request: Request, db: Session = Depends(get_db)):
    persons = db.query(Person).order_by(Person.name_kn).all()
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "stats": family_stats(persons)},
    )

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

@app.get("/profile", response_class=HTMLResponse)
@app.get("/lineage", response_class=HTMLResponse)
async def profile_view(request: Request, db: Session = Depends(get_db)):
    persons = db.query(Person).order_by(Person.name_kn).all()
    return templates.TemplateResponse("lineage.html", {"request": request, "persons": persons})

@app.get("/api/profile/{person_id}")
@app.get("/api/lineage/{person_id}")
async def get_lineage(person_id: int, db: Session = Depends(get_db)):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        return {
            "error": "Person not found.",
            "relationship_en": "Person not found.",
            "relationship_kn": "ವ್ಯಕ್ತಿ ಕಂಡುಬಂದಿಲ್ಲ.",
        }

    persons = db.query(Person).order_by(Person.name_kn).all()
    levels = get_generation_levels(persons)
    ancestors = []
    current = person
    while current:
        ancestors.append(person_payload(current, selected=current.id == person.id))
        current = db.query(Person).filter(Person.id == current.parent_id).first() if current.parent_id else None

    ancestors.reverse()
    tree = person_tree(person, db, selected_id=person.id)
    return {
        "selected_id": person.id,
        "profile": profile_payload(person, db, persons, levels, ancestors, tree),
        "path": ancestors,
        "tree": tree,
    }

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
    relation_label_en = relationship_label(relation_en)

    if relation_en == "same person":
        relationship_en = f"{person1.name_en} and {person2.name_en} are the same person."
        relationship_kn = f"{person1.name_kn} ಮತ್ತು {person2.name_kn} ಅದೇ ವ್ಯಕ್ತಿ."
    elif relation_en == "not directly related":
        relationship_en = f"{person1.name_en} and {person2.name_en} are not directly related."
        relationship_kn = f"{person1.name_kn} ಮತ್ತು {person2.name_kn} ಅವರಿಗೆ ನೇರ ಸಂಬಂಧ ಇಲ್ಲ."
    else:
        relationship_en = f"{person1.name_en} is {person2.name_en}'s {relation_label_en}."
        relationship_kn = f"{person1.name_kn} ಅವರು {person2.name_kn} ಅವರ {relation_kn}."

    return {
        "relationship_en": relationship_en,
        "relationship_kn": relationship_kn,
        "relation_en": relation_en,
        "relation_label_en": relation_label_en,
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

def person_tree(person, db, selected_id=None, visited=None):
    visited = visited or set()
    if person.id in visited:
        return person_payload(person, selected=person.id == selected_id)

    visited.add(person.id)
    payload = person_payload(person, selected=person.id == selected_id)
    children = db.query(Person).filter(Person.parent_id == person.id).order_by(Person.name_kn).all()
    payload["children"] = [
        person_tree(child, db, selected_id=selected_id, visited=visited.copy())
        for child in children
    ]
    return payload

def flatten_tree(tree):
    nodes = []
    for child in tree.get("children", []):
        nodes.append(child)
        nodes.extend(flatten_tree(child))
    return nodes

def max_descendant_depth(tree):
    children = tree.get("children", [])
    if not children:
        return 0
    return 1 + max(max_descendant_depth(child) for child in children)

def family_stats(persons):
    levels = get_generation_levels(persons)
    children_by_parent = {}
    people_by_id = {person.id: person for person in persons}

    for person in persons:
        if person.parent_id:
            children_by_parent.setdefault(person.parent_id, []).append(person)

    generation_counts = {}
    for person in persons:
        generation = levels.get(person.id, 0) + 1
        generation_counts[generation] = generation_counts.get(generation, 0) + 1

    roots = [person for person in persons if not person.parent_id or person.parent_id not in people_by_id]
    leaf_members = [person for person in persons if person.id not in children_by_parent]
    members_with_children = [person for person in persons if person.id in children_by_parent]
    max_children = max((len(children) for children in children_by_parent.values()), default=0)
    largest_families = [
        {
            "person": person_payload(person),
            "children_count": len(children_by_parent.get(person.id, [])),
        }
        for person in members_with_children
        if len(children_by_parent.get(person.id, [])) == max_children
    ]

    gender_counts = {
        "male": sum(1 for person in persons if person.gender == "Male"),
        "female": sum(1 for person in persons if person.gender == "Female"),
    }
    gender_counts["not_recorded"] = len(persons) - gender_counts["male"] - gender_counts["female"]

    generation_list = [
        {"generation": generation, "count": count}
        for generation, count in sorted(generation_counts.items())
    ]
    largest_generation = max(generation_list, key=lambda item: item["count"], default={"generation": 0, "count": 0})

    return {
        "total_people": len(persons),
        "total_generations": max(levels.values(), default=-1) + 1 if persons else 0,
        "root_count": len(roots),
        "roots": [person_payload(person) for person in roots],
        "direct_relationships": sum(1 for person in persons if person.parent_id),
        "members_with_children": len(members_with_children),
        "leaf_members": len(leaf_members),
        "average_children": round(
            sum(len(children) for children in children_by_parent.values()) / len(members_with_children),
            1,
        ) if members_with_children else 0,
        "max_children": max_children,
        "largest_families": largest_families[:8],
        "generation_counts": generation_list,
        "largest_generation": largest_generation,
        "gender_counts": gender_counts,
        "names_with_english": sum(1 for person in persons if person.name_en),
        "names_with_kannada": sum(1 for person in persons if person.name_kn),
    }

def profile_payload(person, db, persons, levels, ancestors, tree):
    people_by_id = {known_person.id: known_person for known_person in persons}
    parent = people_by_id.get(person.parent_id) if person.parent_id else None
    children = [known_person for known_person in persons if known_person.parent_id == person.id]
    siblings = []
    if person.parent_id:
        siblings = [
            known_person
            for known_person in persons
            if known_person.parent_id == person.parent_id and known_person.id != person.id
        ]

    descendants = flatten_tree(tree)
    relation_counts = {}
    related_people = 0
    selected_ancestors = ancestor_distances_from_map(person, people_by_id)
    for relative in persons:
        if relative.id == person.id:
            continue

        relation_en, _ = describe_relationship_from_distances(
            relative,
            person,
            ancestor_distances_from_map(relative, people_by_id),
            selected_ancestors,
        )
        if relation_en == "not directly related":
            continue

        related_people += 1
        relation_counts[relation_en] = relation_counts.get(relation_en, 0) + 1

    return {
        "person": person_payload(person, selected=True),
        "parent": person_payload(parent) if parent else None,
        "children": [person_payload(child) for child in children],
        "siblings": [person_payload(sibling) for sibling in siblings],
        "brothers": [person_payload(sibling) for sibling in siblings if sibling.gender == "Male"],
        "sisters": [person_payload(sibling) for sibling in siblings if sibling.gender == "Female"],
        "unknown_gender_siblings": [
            person_payload(sibling)
            for sibling in siblings
            if sibling.gender not in {"Male", "Female"}
        ],
        "generation": levels.get(person.id, 0) + 1,
        "generation_index": levels.get(person.id, 0),
        "total_family_generations": max(levels.values(), default=0) + 1,
        "ancestor_count": max(len(ancestors) - 1, 0),
        "lineage_depth": len(ancestors),
        "direct_children_count": len(children),
        "descendant_count": len(descendants),
        "descendant_generations": max_descendant_depth(tree),
        "sibling_count": len(siblings),
        "brother_count": sum(1 for sibling in siblings if sibling.gender == "Male"),
        "sister_count": sum(1 for sibling in siblings if sibling.gender == "Female"),
        "unknown_gender_sibling_count": sum(1 for sibling in siblings if sibling.gender not in {"Male", "Female"}),
        "known_relation_count": related_people,
        "relation_summary": [
            {
                "relationship": relationship,
                "label": relationship_label(relationship),
                "label_kn": relationship_label_kn(relationship),
                "description": relationship_description(relationship),
                "description_kn": relationship_description_kn(relationship),
                "count": count,
            }
            for relationship, count in sorted(relation_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
    }

def ancestor_distances_from_map(person, people_by_id):
    distances = {}
    distance = 0
    current = person

    while current and current.id not in distances:
        distances[current.id] = distance
        current = people_by_id.get(current.parent_id) if current.parent_id else None
        distance += 1

    return distances

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

def removed_generations(relationship):
    if " once removed" in relationship:
        return 1
    if " twice removed" in relationship:
        return 2
    marker = " times removed"
    if marker in relationship:
        before_marker = relationship.split(marker, 1)[0]
        number = before_marker.split()[-1]
        if number.isdigit():
            return int(number)
    return 0

def relationship_label(relationship):
    if "cousin" not in relationship:
        return relationship.replace("-", " ").capitalize()

    generation_gap = removed_generations(relationship)
    cousin_type = relationship
    for suffix in (" once removed", " twice removed"):
        cousin_type = cousin_type.replace(suffix, "")
    if " times removed" in cousin_type:
        parts = cousin_type.split()
        cousin_type = " ".join(parts[:-3])

    cousin_label = cousin_type.capitalize()
    if generation_gap == 0:
        return f"{cousin_label}, same generation"
    if generation_gap == 1:
        return f"{cousin_label}, 1 generation apart"
    return f"{cousin_label}, {generation_gap} generations apart"

def relationship_description(relationship):
    if "cousin" in relationship:
        generation_gap = removed_generations(relationship)
        if generation_gap == 0:
            return "Same generation in that cousin branch."
        if generation_gap == 1:
            return "One person is one generation above or below the other."
        return f"One person is {generation_gap} generations above or below the other."

    descriptions = {
        "sibling": "Same parent.",
        "brother": "Male sibling with the same parent.",
        "sister": "Female sibling with the same parent.",
        "parent": "Direct parent.",
        "child": "Direct child.",
        "grandparent": "Parent of a parent.",
        "grandchild": "Child of a child.",
        "aunt/uncle": "Sibling of a parent, or same-generation equivalent in the tree.",
        "niece/nephew": "Child of a sibling, or next-generation equivalent in the tree.",
    }
    return descriptions.get(relationship, "")

def relationship_label_kn(relationship):
    labels = {
        "same person": "ಅದೇ ವ್ಯಕ್ತಿ",
        "not directly related": "ನೇರ ಸಂಬಂಧ ಇಲ್ಲ",
        "sibling": "ಸಹೋದರ/ಸಹೋದರಿ",
        "brother": "ಸಹೋದರ",
        "sister": "ಸಹೋದರಿ",
        "parent": "ಪೋಷಕರು",
        "father": "ತಂದೆ",
        "mother": "ತಾಯಿ",
        "child": "ಮಗು",
        "son": "ಮಗ",
        "daughter": "ಮಗಳು",
        "grandparent": "ಅಜ್ಜ/ಅಜ್ಜಿ",
        "grandfather": "ಅಜ್ಜ",
        "grandmother": "ಅಜ್ಜಿ",
        "grandchild": "ಮೊಮ್ಮಗು",
        "grandson": "ಮೊಮ್ಮಗ",
        "granddaughter": "ಮೊಮ್ಮಗಳು",
        "aunt/uncle": "ಅತ್ತೆ/ಮಾಮ",
        "uncle": "ಮಾಮ/ಚಿಕ್ಕಪ್ಪ",
        "aunt": "ಅತ್ತೆ/ಚಿಕ್ಕಮ್ಮ",
        "niece/nephew": "ಸೋದರ ಮಗು",
        "nephew": "ಸೋದರಳಿಯ",
        "niece": "ಸೋದರ ಸೊಸೆ",
    }
    if relationship in labels:
        return labels[relationship]
    if "cousin" in relationship:
        generation_gap = removed_generations(relationship)
        cousin_label = "ಸೋದರ ಸಂಬಂಧಿ"
        if generation_gap == 0:
            return f"{cousin_label}, ಅದೇ ತಲೆಮಾರು"
        return f"{cousin_label}, {generation_gap} ತಲೆಮಾರಿನ ಅಂತರ"
    if relationship.startswith("great-"):
        if "grandfather" in relationship:
            return "ಹಿರಿಯ ಪೂರ್ವಜ"
        if "grandmother" in relationship:
            return "ಹಿರಿಯ ಪೂರ್ವಜೆ"
        if "grandparent" in relationship:
            return "ಹಿರಿಯ ಪೂರ್ವಜರು"
        if "grandson" in relationship:
            return "ಮುಂದಿನ ವಂಶಜ"
        if "granddaughter" in relationship:
            return "ಮುಂದಿನ ವಂಶಜೆ"
        if "grandchild" in relationship:
            return "ಮುಂದಿನ ವಂಶಜರು"
        if "uncle" in relationship or "aunt" in relationship:
            return "ಹಿರಿಯ ಅತ್ತೆ/ಮಾಮ"
        if "nephew" in relationship or "niece" in relationship:
            return "ಮುಂದಿನ ತಲೆಮಾರಿನ ಸೋದರ ಮಗು"
    return relationship

def relationship_description_kn(relationship):
    if "cousin" in relationship:
        generation_gap = removed_generations(relationship)
        if generation_gap == 0:
            return "ಆ ಸೋದರ ಸಂಬಂಧದ ಶಾಖೆಯಲ್ಲಿ ಅದೇ ತಲೆಮಾರು."
        if generation_gap == 1:
            return "ಒಬ್ಬರು ಮತ್ತೊಬ್ಬರಿಗಿಂತ ಒಂದು ತಲೆಮಾರು ಮೇಲೆ ಅಥವಾ ಕೆಳಗೆ ಇದ್ದಾರೆ."
        return f"ಒಬ್ಬರು ಮತ್ತೊಬ್ಬರಿಗಿಂತ {generation_gap} ತಲೆಮಾರು ಮೇಲೆ ಅಥವಾ ಕೆಳಗೆ ಇದ್ದಾರೆ."

    descriptions = {
        "sibling": "ಅದೇ ಪೋಷಕರ ಮಕ್ಕಳು.",
        "brother": "ಅದೇ ಪೋಷಕರ ಪುರುಷ ಸಹೋದರ.",
        "sister": "ಅದೇ ಪೋಷಕರ ಮಹಿಳಾ ಸಹೋದರಿ.",
        "parent": "ನೇರ ಪೋಷಕರು.",
        "child": "ನೇರ ಮಗು.",
        "grandparent": "ಪೋಷಕರ ಪೋಷಕರು.",
        "grandchild": "ಮಕ್ಕಳ ಮಗು.",
        "aunt/uncle": "ಪೋಷಕರ ಸಹೋದರ/ಸಹೋದರಿ ಅಥವಾ ವೃಕ್ಷದಲ್ಲಿನ ಅದೇ ತಲೆಮಾರಿನ ಸಂಬಂಧಿ.",
        "niece/nephew": "ಸಹೋದರ/ಸಹೋದರಿಯ ಮಗು ಅಥವಾ ಮುಂದಿನ ತಲೆಮಾರಿನ ಸಮಾನ ಸಂಬಂಧಿ.",
    }
    return descriptions.get(relationship, "")

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

    return describe_relationship_from_distances(person1, person2, ancestors1, ancestors2)

def describe_relationship_from_distances(person1, person2, ancestors1, ancestors2):
    if person1.id == person2.id:
        return "same person", "ಅದೇ ವ್ಯಕ್ತಿ"

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
