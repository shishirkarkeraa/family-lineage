from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqladmin import Admin, ModelView
from database import engine, get_db, migrate_person_name_columns, Person

app = FastAPI(title="Family Tree")

@app.on_event("startup")
def startup():
    migrate_person_name_columns()

admin = Admin(app, engine)

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
    nodes = [{"id": p.id, "name_en": p.name_en, "name_kn": p.name_kn, "gender": p.gender} for p in persons]
    edges = [{"from": p.parent_id, "to": p.id} for p in persons if p.parent_id]
    return templates.TemplateResponse("tree.html", {"request": request, "nodes": nodes, "edges": edges})

@app.get("/lineage", response_class=HTMLResponse)
async def lineage_view(request: Request, db: Session = Depends(get_db)):
    persons = db.query(Person).order_by(Person.name_kn).all()
    return templates.TemplateResponse("lineage.html", {"request": request, "persons": persons})

@app.get("/api/lineage/{person_id}")
async def get_lineage(person_id: int, db: Session = Depends(get_db)):
    def get_descendants(pid):
        children = db.query(Person).filter(Person.parent_id == pid).all()
        result = []
        for child in children:
            result.append({"id": child.id, "name_en": child.name_en, "name_kn": child.name_kn, "gender": child.gender, "children": get_descendants(child.id)})
        return result
    
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        return {"error": "Person not found"}
        
    lineage_tree = {
        "id": person.id,
        "name_en": person.name_en,
        "name_kn": person.name_kn,
        "gender": person.gender,
        "children": get_descendants(person.id)
    }
    return lineage_tree

@app.get("/relationship", response_class=HTMLResponse)
async def relationship_view(request: Request, db: Session = Depends(get_db)):
    persons = db.query(Person).order_by(Person.name_kn).all()
    return templates.TemplateResponse("relationship.html", {"request": request, "persons": persons})

@app.get("/api/relationship")
async def get_relationship(p1_id: int, p2_id: int, db: Session = Depends(get_db)):
    def get_ancestors(pid):
        path = []
        curr = db.query(Person).filter(Person.id == pid).first()
        while curr:
            path.append({"id": curr.id, "name_en": curr.name_en, "name_kn": curr.name_kn})
            if curr.parent_id:
                curr = db.query(Person).filter(Person.id == curr.parent_id).first()
            else:
                break
        return path
        
    path1 = get_ancestors(p1_id)
    path2 = get_ancestors(p2_id)
    
    path1.reverse()
    path2.reverse()

    common = []
    i = 0
    while i < len(path1) and i < len(path2) and path1[i]["id"] == path2[i]["id"]:
        common.append(path1[i])
        i += 1
        
    if not common:
        return {
            "relationship_en": "No common ancestor found.",
            "relationship_kn": "ಸಾಮಾನ್ಯ ಪೂರ್ವಜರು ಕಂಡುಬಂದಿಲ್ಲ.",
            "path": [],
        }
        
    lca = common[-1]
    
    full_path = path1[i-1:]
    full_path.reverse()
    full_path.extend(path2[i:])
    
    return {
        "relationship_en": f"Common ancestor is {lca['name_en']}",
        "relationship_kn": f"ಸಾಮಾನ್ಯ ಪೂರ್ವಜರು {lca['name_kn']}",
        "path": full_path,
        "lca": lca
    }
