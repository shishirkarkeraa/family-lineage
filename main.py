import os
import json
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode, urlparse
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import URLError

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from database import (
    CHANGE_ADD_DESCENDANT,
    CHANGE_APPROVED,
    CHANGE_EDIT_PERSON,
    CHANGE_PENDING,
    CHANGE_REJECTED,
    GENDER_FEMALE,
    GENDER_MALE,
    GENDER_VALUES,
    AdminCredential,
    ChangeRequest,
    Person,
    User,
    get_db,
    migrate_person_name_columns,
    normalize_gender,
)

app = FastAPI(title="Karkera Family")

@app.on_event("startup")
def startup():
    if os.getenv("VERCEL") and os.getenv("RUN_DB_MIGRATIONS_ON_STARTUP") != "1":
        return
    migrate_person_name_columns()

PUBLIC_EXEMPT_PREFIXES = ("/admin", "/auth", "/static")
PUBLIC_EXEMPT_PATHS = {"/favicon.ico", "/favicon.png", "/robots.txt"}

@app.middleware("http")
async def require_public_google_login(request: Request, call_next):
    path = request.url.path
    is_exempt = path in PUBLIC_EXEMPT_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_EXEMPT_PREFIXES)
    if is_exempt or request.session.get("public_user_id"):
        return await call_next(request)

    if path.startswith("/api"):
        return JSONResponse({"error": "Google login is required."}, status_code=401)

    login_url = "/auth/login?" + urlencode({"next": str(request.url)})
    return RedirectResponse(login_url, status_code=303)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("ADMIN_SESSION_SECRET", "family-lineage-admin"),
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
PERSON_CACHE_SECONDS = int(os.getenv("PERSON_CACHE_SECONDS", "60"))
_person_cache = {"expires_at": 0, "persons": None}

def get_cached_persons(db):
    now = time.monotonic()
    if _person_cache["persons"] is not None and _person_cache["expires_at"] > now:
        return _person_cache["persons"]

    persons = db.query(Person).order_by(Person.name_kn).all()
    _person_cache["persons"] = persons
    _person_cache["expires_at"] = now + PERSON_CACHE_SECONDS
    return persons

def clear_person_cache():
    _person_cache["expires_at"] = 0
    _person_cache["persons"] = None

def admin_required(request: Request):
    if not request.session.get("admin_logged_in"):
        return RedirectResponse("/admin/login", status_code=303)
    return None

def public_user_required(request: Request, db: Session):
    user_id = request.session.get("public_user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Google login is required")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session.pop("public_user_id", None)
        raise HTTPException(status_code=401, detail="Google login is required")
    return user

def app_base_url(request: Request):
    configured_url = os.getenv("APP_BASE_URL")
    if configured_url:
        return configured_url.rstrip("/")

    vercel_url = os.getenv("VERCEL_URL")
    if vercel_url:
        return f"https://{vercel_url}".rstrip("/")

    return str(request.base_url).rstrip("/")

def google_redirect_uri(request: Request):
    return os.getenv("GOOGLE_REDIRECT_URI") or f"{app_base_url(request)}/auth/callback"

def safe_next_url(next_url, request: Request):
    if not next_url:
        return "/"

    parsed = urlparse(next_url)
    if not parsed.netloc and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url

    app_origin = urlparse(app_base_url(request))
    if parsed.scheme in {"http", "https"} and parsed.netloc == app_origin.netloc:
        return next_url

    return "/"

def fetch_json(url, data=None, headers=None):
    body = urlencode(data).encode("utf-8") if data else None
    request = UrlRequest(url, data=body, headers=headers or {})
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))

def upsert_google_user(db, profile):
    google_sub = profile.get("sub")
    email = profile.get("email")
    if not google_sub or not email:
        raise HTTPException(status_code=400, detail="Google profile did not include required identity data")

    user = db.query(User).filter(User.google_sub == google_sub).first()
    now = datetime.utcnow()
    if not user:
        user = User(google_sub=google_sub, email=email, created_at=now)
        db.add(user)

    user.email = email
    user.name = profile.get("name")
    user.picture = profile.get("picture")
    user.last_login_at = now
    db.commit()
    db.refresh(user)
    return user

def person_option_label(person):
    names = [name for name in (person.name_en, person.name_kn) if name]
    return " / ".join(names) or f"Person #{person.id}"

def parse_optional_int(value):
    return int(value) if value else None

def parse_optional_date(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()

def person_form_payload(name_en, name_kn, gender, parent_id, parent2_id, date_of_birth):
    return {
        "name_en": name_en.strip() if name_en else None,
        "name_kn": name_kn.strip() if name_kn else None,
        "gender": normalize_gender(gender),
        "parent_id": parse_optional_int(parent_id),
        "parent2_id": parse_optional_int(parent2_id),
        "date_of_birth": parse_optional_date(date_of_birth),
    }

def assign_person_form(person, payload):
    person.name_en = payload["name_en"]
    person.name_kn = payload["name_kn"]
    person.gender = payload["gender"]
    person.parent_id = payload["parent_id"]
    person.parent2_id = payload["parent2_id"]
    person.date_of_birth = payload["date_of_birth"]

def basic_person_payload(name_en, name_kn, gender, date_of_birth):
    parsed_date = parse_optional_date(date_of_birth)
    return {
        "name_en": name_en.strip() if name_en else None,
        "name_kn": name_kn.strip() if name_kn else None,
        "gender": normalize_gender(gender),
        "date_of_birth": parsed_date.isoformat() if parsed_date else None,
    }

def apply_basic_person_payload(person, payload):
    person.name_en = payload.get("name_en")
    person.name_kn = payload.get("name_kn")
    person.gender = payload.get("gender")
    person.date_of_birth = parse_optional_date(payload.get("date_of_birth"))

def display_change_value(value):
    return value if value not in (None, "") else "-"

def basic_person_current_values(person):
    return {
        "name_en": person.name_en,
        "name_kn": person.name_kn,
        "gender": person.gender,
        "date_of_birth": person.date_of_birth.isoformat() if person.date_of_birth else None,
    }

def pending_change_rows(db):
    changes = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.status == CHANGE_PENDING)
        .order_by(ChangeRequest.created_at.desc(), ChangeRequest.id.desc())
        .all()
    )
    field_labels = {
        "name_en": "Name EN",
        "name_kn": "Name KN",
        "gender": "Gender",
        "date_of_birth": "Date of birth",
        "parent_id": "Parent",
    }
    return [
        {
            "id": change.id,
            "change_type": change.change_type,
            "change_label": "Add descendant" if change.change_type == CHANGE_ADD_DESCENDANT else "Edit person",
            "requester": change.requester_name or change.requester_email or "Unknown",
            "requester_email": change.requester_email,
            "created_at": change.created_at,
            "target": person_option_label(change.target_person) if change.target_person else None,
            "parent": person_option_label(change.parent_person) if change.parent_person else None,
            "submitted": [
                {"label": field_labels.get(key, key), "value": display_change_value(value)}
                for key, value in (change.payload or {}).items()
                if key in field_labels
            ],
            "current": [
                {"label": field_labels.get(key, key), "value": display_change_value(value)}
                for key, value in (basic_person_current_values(change.target_person).items() if change.target_person else [])
            ] if change.change_type == CHANGE_EDIT_PERSON else [],
        }
        for change in changes
    ]

def admin_people(db):
    people = db.query(Person).order_by(Person.name_en, Person.name_kn, Person.id).all()
    people_by_id = {person.id: person for person in people}
    return [
        {
            "id": person.id,
            "name_en": person.name_en,
            "name_kn": person.name_kn,
            "gender": person.gender,
            "date_of_birth": person.date_of_birth,
            "parent": person_option_label(parent) if (parent := people_by_id.get(person.parent_id)) else None,
            "parent2": person_option_label(parent) if (parent := people_by_id.get(person.parent2_id)) else None,
        }
        for person in people
    ]

def admin_parent_options(db, exclude_person_id=None):
    query = db.query(Person)
    if exclude_person_id is not None:
        query = query.filter(Person.id != exclude_person_id)
    return query.order_by(Person.name_en, Person.name_kn, Person.id).all()

@app.get("/auth/login")
async def google_login(request: Request, next: str = "/"):
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        return HTMLResponse("Google login is not configured.", status_code=500)

    state = secrets.token_urlsafe(24)
    request.session["google_oauth_state"] = state
    request.session["google_oauth_next"] = safe_next_url(next, request)

    params = {
        "client_id": client_id,
        "redirect_uri": google_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), status_code=303)

@app.get("/auth/callback")
async def google_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    expected_state = request.session.pop("google_oauth_state", None)
    next_url = request.session.pop("google_oauth_next", "/")
    if not state or state != expected_state or not code:
        return HTMLResponse("Google login could not be verified.", status_code=400)

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return HTMLResponse("Google login is not configured.", status_code=500)

    try:
        token_payload = fetch_json(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": google_redirect_uri(request),
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        access_token = token_payload.get("access_token")
        if not access_token:
            return HTMLResponse("Google login did not return an access token.", status_code=400)

        profile = fetch_json(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except (URLError, TimeoutError, json.JSONDecodeError):
        return HTMLResponse("Google login failed. Please try again.", status_code=502)

    user = upsert_google_user(db, profile)
    request.session["public_user_id"] = user.id
    request.session["public_user_email"] = user.email
    request.session["public_user_name"] = user.name
    return RedirectResponse(next_url or "/", status_code=303)

@app.get("/auth/logout")
async def google_logout(request: Request):
    request.session.pop("public_user_id", None)
    request.session.pop("public_user_email", None)
    request.session.pop("public_user_name", None)
    return RedirectResponse("/auth/login", status_code=303)

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    if request.session.get("admin_logged_in"):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": None})

@app.post("/admin/login")
async def admin_login_post(
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    credential = db.query(AdminCredential).first()
    if credential and password == credential.password:
        request.session["admin_logged_in"] = True
        return RedirectResponse("/admin", status_code=303)

    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "error": "Invalid admin password."},
        status_code=401,
    )

@app.post("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "people": admin_people(db),
            "parents": admin_parent_options(db),
            "pending_changes": pending_change_rows(db),
        },
    )

@app.get("/admin/users/new", response_class=HTMLResponse)
async def admin_new_user(request: Request, db: Session = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    return templates.TemplateResponse(
        "admin_person_form.html",
        {"request": request, "person": None, "parents": admin_parent_options(db), "error": None},
    )

@app.post("/admin/users/new")
async def admin_create_user(
    request: Request,
    name_en: str = Form(""),
    name_kn: str = Form(""),
    gender: str = Form(""),
    parent_id: str = Form(""),
    parent2_id: str = Form(""),
    date_of_birth: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    try:
        person = Person()
        assign_person_form(person, person_form_payload(name_en, name_kn, gender, parent_id, parent2_id, date_of_birth))
        db.add(person)
        db.commit()
        clear_person_cache()
        return RedirectResponse("/admin", status_code=303)
    except ValueError:
        return templates.TemplateResponse(
            "admin_person_form.html",
            {"request": request, "person": None, "parents": admin_parent_options(db), "error": "Use a valid date of birth and gender."},
            status_code=400,
        )

@app.get("/admin/users/{person_id}/edit", response_class=HTMLResponse)
async def admin_edit_user(request: Request, person_id: int, db: Session = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    return templates.TemplateResponse(
        "admin_person_form.html",
        {"request": request, "person": person, "parents": admin_parent_options(db, person_id), "error": None},
    )

@app.post("/admin/users/{person_id}/edit")
async def admin_update_user(
    request: Request,
    person_id: int,
    name_en: str = Form(""),
    name_kn: str = Form(""),
    gender: str = Form(""),
    parent_id: str = Form(""),
    parent2_id: str = Form(""),
    date_of_birth: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    try:
        assign_person_form(person, person_form_payload(name_en, name_kn, gender, parent_id, parent2_id, date_of_birth))
        db.commit()
        clear_person_cache()
        return RedirectResponse("/admin", status_code=303)
    except ValueError:
        return templates.TemplateResponse(
            "admin_person_form.html",
            {"request": request, "person": person, "parents": admin_parent_options(db, person_id), "error": "Use a valid date of birth and gender."},
            status_code=400,
        )

@app.post("/admin/changes/{change_id}/approve")
async def admin_approve_change(request: Request, change_id: int, db: Session = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    change = db.query(ChangeRequest).filter(
        ChangeRequest.id == change_id,
        ChangeRequest.status == CHANGE_PENDING,
    ).first()
    if not change:
        raise HTTPException(status_code=404, detail="Pending change not found")

    if change.change_type == CHANGE_ADD_DESCENDANT:
        person = Person(parent_id=change.parent_person_id)
        apply_basic_person_payload(person, change.payload)
        person.parent_id = change.payload.get("parent_id") or change.parent_person_id
        db.add(person)
    elif change.change_type == CHANGE_EDIT_PERSON:
        person = db.query(Person).filter(Person.id == change.target_person_id).first()
        if not person:
            raise HTTPException(status_code=404, detail="Target person not found")
        apply_basic_person_payload(person, change.payload)
    else:
        raise HTTPException(status_code=400, detail="Unsupported change type")

    change.status = CHANGE_APPROVED
    change.reviewed_at = datetime.utcnow()
    change.updated_at = datetime.utcnow()
    db.commit()
    clear_person_cache()
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/changes/{change_id}/reject")
async def admin_reject_change(
    request: Request,
    change_id: int,
    reviewer_note: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    change = db.query(ChangeRequest).filter(
        ChangeRequest.id == change_id,
        ChangeRequest.status == CHANGE_PENDING,
    ).first()
    if not change:
        raise HTTPException(status_code=404, detail="Pending change not found")

    change.status = CHANGE_REJECTED
    change.reviewer_note = reviewer_note.strip() or None
    change.reviewed_at = datetime.utcnow()
    change.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/admin", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, db: Session = Depends(get_db)):
    persons = get_cached_persons(db)
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "stats": family_stats(persons)},
    )

@app.get("/tree", response_class=HTMLResponse)
async def tree_view(request: Request, db: Session = Depends(get_db)):
    persons = get_cached_persons(db)
    levels = get_generation_levels(persons)
    nodes = [
        {
            "id": p.id,
            "name_en": p.name_en,
            "name_kn": p.name_kn,
            "gender": p.gender,
            "date_of_birth": p.date_of_birth.isoformat() if p.date_of_birth else None,
            "level": levels.get(p.id, 0),
        }
        for p in persons
    ]
    edges = [{"from": p.parent_id, "to": p.id} for p in persons if p.parent_id]
    return templates.TemplateResponse("tree.html", {"request": request, "nodes": nodes, "edges": edges})

@app.post("/api/changes/add-descendant")
async def request_add_descendant(
    request: Request,
    parent_id: int = Form(...),
    name_en: str = Form(""),
    name_kn: str = Form(""),
    gender: str = Form(""),
    date_of_birth: str = Form(""),
    db: Session = Depends(get_db),
):
    user = public_user_required(request, db)
    parent = db.query(Person).filter(Person.id == parent_id).first()
    if not parent:
        return JSONResponse({"error_key": "person_not_found"}, status_code=404)

    try:
        payload = basic_person_payload(name_en, name_kn, gender, date_of_birth)
    except ValueError:
        return JSONResponse({"error_key": "invalid_date_gender"}, status_code=400)

    if not payload["name_en"] and not payload["name_kn"]:
        return JSONResponse({"error_key": "enter_one_name"}, status_code=400)

    payload["parent_id"] = parent.id
    change = ChangeRequest(
        change_type=CHANGE_ADD_DESCENDANT,
        status=CHANGE_PENDING,
        parent_person_id=parent.id,
        payload=payload,
        requester_user_id=user.id,
        requester_email=user.email,
        requester_name=user.name,
    )
    db.add(change)
    db.commit()
    return {"message": "Your edit has been submitted and will be reflected after review."}

@app.post("/api/changes/edit-person")
async def request_edit_person(
    request: Request,
    person_id: int = Form(...),
    name_en: str = Form(""),
    name_kn: str = Form(""),
    gender: str = Form(""),
    date_of_birth: str = Form(""),
    db: Session = Depends(get_db),
):
    user = public_user_required(request, db)
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        return JSONResponse({"error_key": "person_not_found"}, status_code=404)

    try:
        payload = basic_person_payload(name_en, name_kn, gender, date_of_birth)
    except ValueError:
        return JSONResponse({"error_key": "invalid_date_gender"}, status_code=400)

    current = basic_person_current_values(person)
    if all(payload[key] == current[key] for key in payload):
        return JSONResponse({"error_key": "no_changes_submitted"}, status_code=400)

    change = ChangeRequest(
        change_type=CHANGE_EDIT_PERSON,
        status=CHANGE_PENDING,
        target_person_id=person.id,
        payload=payload,
        requester_user_id=user.id,
        requester_email=user.email,
        requester_name=user.name,
    )
    db.add(change)
    db.commit()
    return {"message": "Your edit has been submitted and will be reflected after review."}

@app.get("/profile", response_class=HTMLResponse)
@app.get("/lineage", response_class=HTMLResponse)
async def profile_view(request: Request, db: Session = Depends(get_db)):
    persons = get_cached_persons(db)
    return templates.TemplateResponse("lineage.html", {"request": request, "persons": persons})

@app.get("/api/profile/{person_id}")
@app.get("/api/lineage/{person_id}")
async def get_lineage(person_id: int, db: Session = Depends(get_db)):
    persons = get_cached_persons(db)
    people_by_id = people_by_id_map(persons)
    person = people_by_id.get(person_id)
    if not person:
        return {
            "error": "Person not found.",
            "relationship_en": "Person not found.",
            "relationship_kn": "ವ್ಯಕ್ತಿ ಕಂಡುಬಂದಿಲ್ಲ.",
        }

    levels = get_generation_levels(persons)
    children_by_parent = children_by_parent_map(persons)
    ancestors = ancestor_payloads_from_map(person, people_by_id)
    tree = person_tree_from_map(person, children_by_parent, selected_id=person.id)
    return {
        "selected_id": person.id,
        "profile": profile_payload(person, persons, levels, ancestors, tree, people_by_id, children_by_parent),
        "path": ancestors,
        "tree": tree,
    }

def ancestor_payloads_from_map(person, people_by_id):
    ancestors = []
    seen = set()
    current = person

    while current and current.id not in seen:
        seen.add(current.id)
        ancestors.append(person_payload(current, selected=current.id == person.id))
        current = people_by_id.get(current.parent_id) if current.parent_id else None

    ancestors.reverse()
    return ancestors

@app.get("/relationship", response_class=HTMLResponse)
async def relationship_view(request: Request, db: Session = Depends(get_db)):
    persons = get_cached_persons(db)
    return templates.TemplateResponse("relationship.html", {"request": request, "persons": persons})

@app.get("/api/relationship")
async def get_relationship(p1_id: int, p2_id: int, db: Session = Depends(get_db)):
    persons = get_cached_persons(db)
    people_by_id = people_by_id_map(persons)
    person1 = people_by_id.get(p1_id)
    person2 = people_by_id.get(p2_id)

    if not person1 or not person2:
        return {
            "relationship_en": "Person not found.",
            "relationship_kn": "ವ್ಯಕ್ತಿ ಕಂಡುಬಂದಿಲ್ಲ.",
        }

    relation_en, relation_kn = describe_relationship_from_map(person1, person2, people_by_id)
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

def people_by_id_map(persons):
    return {person.id: person for person in persons}

def children_by_parent_map(persons):
    children_by_parent = {}
    for person in persons:
        if person.parent_id:
            children_by_parent.setdefault(person.parent_id, []).append(person)

    for children in children_by_parent.values():
        children.sort(key=lambda person: person.name_kn or person.name_en or "")

    return children_by_parent

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

def person_tree_from_map(person, children_by_parent, selected_id=None, visited=None):
    visited = visited or set()
    if person.id in visited:
        return person_payload(person, selected=person.id == selected_id)

    visited.add(person.id)
    payload = person_payload(person, selected=person.id == selected_id)
    payload["children"] = [
        person_tree_from_map(child, children_by_parent, selected_id=selected_id, visited=visited.copy())
        for child in children_by_parent.get(person.id, [])
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
    children_by_parent = children_by_parent_map(persons)
    people_by_id = people_by_id_map(persons)

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
        "male": sum(1 for person in persons if person.gender == GENDER_MALE),
        "female": sum(1 for person in persons if person.gender == GENDER_FEMALE),
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

def profile_payload(person, persons, levels, ancestors, tree, people_by_id, children_by_parent):
    parent = people_by_id.get(person.parent_id) if person.parent_id else None
    children = children_by_parent.get(person.id, [])
    siblings = [
        known_person
        for known_person in children_by_parent.get(person.parent_id, [])
        if known_person.id != person.id
    ] if person.parent_id else []

    descendants = flatten_tree(tree)
    relation_counts = {}
    related_people = 0
    ancestor_cache = {
        known_person.id: ancestor_distances_from_map(known_person, people_by_id)
        for known_person in persons
    }
    selected_ancestors = ancestor_cache[person.id]
    for relative in persons:
        if relative.id == person.id:
            continue

        relation_en, _ = describe_relationship_from_distances(
            relative,
            person,
            ancestor_cache[relative.id],
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
        "brothers": [person_payload(sibling) for sibling in siblings if sibling.gender == GENDER_MALE],
        "sisters": [person_payload(sibling) for sibling in siblings if sibling.gender == GENDER_FEMALE],
        "unknown_gender_siblings": [
            person_payload(sibling)
            for sibling in siblings
            if sibling.gender not in GENDER_VALUES
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
        "brother_count": sum(1 for sibling in siblings if sibling.gender == GENDER_MALE),
        "sister_count": sum(1 for sibling in siblings if sibling.gender == GENDER_FEMALE),
        "unknown_gender_sibling_count": sum(1 for sibling in siblings if sibling.gender not in GENDER_VALUES),
        "known_relation_count": related_people,
        "relation_summary": simple_relation_summary(relation_counts),
    }

def simple_relation_summary(relation_counts):
    category_counts = {}

    for relationship, count in relation_counts.items():
        category = simple_relation_category(relationship)
        category_counts[category] = category_counts.get(category, 0) + count

    return [
        {
            "category": category,
            "count": count,
            "label": simple_relation_label(category, count, "en"),
            "label_kn": simple_relation_label(category, count, "kn"),
        }
        for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], relation_category_order(item[0])))
    ]

def simple_relation_category(relationship):
    if "cousin" in relationship:
        return "cousins"
    if "aunt" in relationship or "uncle" in relationship:
        return "aunts_uncles"
    if "niece" in relationship or "nephew" in relationship:
        return "nieces_nephews"
    if "grandparent" in relationship:
        return "grandparents"
    if "grandchild" in relationship or "grandson" in relationship or "granddaughter" in relationship:
        return "grandchildren"
    if relationship in {"parent", "father", "mother"}:
        return "parents"
    if relationship in {"child", "son", "daughter"}:
        return "children"
    if relationship in {"sibling", "brother", "sister"}:
        return "siblings"
    return "relatives"

def relation_category_order(category):
    order = {
        "cousins": 1,
        "siblings": 2,
        "parents": 3,
        "grandparents": 4,
        "children": 5,
        "grandchildren": 6,
        "aunts_uncles": 7,
        "nieces_nephews": 8,
        "relatives": 9,
    }
    return order.get(category, 99)

def simple_relation_label(category, count, language):
    if language == "kn":
        labels = {
            "cousins": ("ಸೋದರ ಸಂಬಂಧಿ", "ಸೋದರ ಸಂಬಂಧಿಗಳು"),
            "siblings": ("ಸಹೋದರ/ಸಹೋದರಿ", "ಸಹೋದರರು/ಸಹೋದರಿಯರು"),
            "parents": ("ಪೋಷಕ", "ಪೋಷಕರು"),
            "grandparents": ("ಅಜ್ಜ/ಅಜ್ಜಿ", "ಅಜ್ಜ/ಅಜ್ಜಿಯರು"),
            "children": ("ಮಗು", "ಮಕ್ಕಳು"),
            "grandchildren": ("ಮೊಮ್ಮಗು", "ಮೊಮ್ಮಕ್ಕಳು"),
            "aunts_uncles": ("ಅತ್ತೆ/ಮಾಮ", "ಅತ್ತೆ/ಮಾಮಂದಿರು"),
            "nieces_nephews": ("ಸೋದರ ಮಗು", "ಸೋದರ ಮಕ್ಕಳು"),
            "relatives": ("ಸಂಬಂಧಿ", "ಸಂಬಂಧಿಕರು"),
        }
        singular, plural = labels.get(category, labels["relatives"])
        return f"ಇವರಿಗೆ {count} {singular if count == 1 else plural} ಇದ್ದಾರೆ"

    labels = {
        "cousins": ("cousin", "cousins"),
        "siblings": ("sibling", "siblings"),
        "parents": ("parent", "parents"),
        "grandparents": ("grandparent", "grandparents"),
        "children": ("child", "children"),
        "grandchildren": ("grandchild", "grandchildren"),
        "aunts_uncles": ("aunt/uncle", "aunts/uncles"),
        "nieces_nephews": ("niece/nephew", "nieces/nephews"),
        "relatives": ("relative", "relatives"),
    }
    singular, plural = labels.get(category, labels["relatives"])
    return f"Has {count} {singular if count == 1 else plural}"

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

def describe_relationship_from_map(person1, person2, people_by_id):
    if person1.id == person2.id:
        return "same person", "ಅದೇ ವ್ಯಕ್ತಿ"

    ancestors1 = ancestor_distances_from_map(person1, people_by_id)
    ancestors2 = ancestor_distances_from_map(person2, people_by_id)
    relation_en, relation_kn = describe_relationship_from_distances(person1, person2, ancestors1, ancestors2)
    exact_relation_kn = exact_relationship_kn(person1, person2, ancestors1, ancestors2, people_by_id)
    return relation_en, exact_relation_kn or relation_kn

def gendered(person, male, female, neutral):
    if person.gender == GENDER_MALE:
        return male
    if person.gender == GENDER_FEMALE:
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
        ancestor_relation_kn(person, distance),
    )

def descendant_relation(person, distance):
    if distance == 1:
        return gendered(person, "son", "daughter", "child"), gendered(person, "ಮಗ", "ಮಗಳು", "ಮಗು")
    if distance == 2:
        return gendered(person, "grandson", "granddaughter", "grandchild"), gendered(person, "ಮೊಮ್ಮಗ", "ಮೊಮ್ಮಗಳು", "ಮೊಮ್ಮಗು")

    prefix = "great-" * (distance - 2)
    return (
        gendered(person, f"{prefix}grandson", f"{prefix}granddaughter", f"{prefix}grandchild"),
        descendant_relation_kn(person, distance),
    )

def ancestor_relation_kn(person, distance):
    if distance == 1:
        return gendered(person, "ತಂದೆ", "ತಾಯಿ", "ಪೋಷಕರು")
    if distance == 2:
        return gendered(person, "ಅಜ್ಜ", "ಅಜ್ಜಿ", "ಅಜ್ಜ/ಅಜ್ಜಿ")

    prefix = " ".join(["ಮುತ್ತ"] * (distance - 2))
    return gendered(person, f"{prefix}ಜ್ಜ", f"{prefix}ಜ್ಜಿ", f"{prefix} ಅಜ್ಜ/ಅಜ್ಜಿ")

def descendant_relation_kn(person, distance):
    if distance == 1:
        return gendered(person, "ಮಗ", "ಮಗಳು", "ಮಗು")
    if distance == 2:
        return gendered(person, "ಮೊಮ್ಮಗ", "ಮೊಮ್ಮಗಳು", "ಮೊಮ್ಮಗು")

    prefix = " ".join(["ಮರಿ"] * (distance - 2))
    return gendered(person, f"{prefix} ಮೊಮ್ಮಗ", f"{prefix} ಮೊಮ್ಮಗಳು", f"{prefix} ಮೊಮ್ಮಗು")

def ordinal_kn(value):
    names = {
        1: "ಮೊದಲ",
        2: "ಎರಡನೇ",
        3: "ಮೂರನೇ",
        4: "ನಾಲ್ಕನೇ",
        5: "ಐದನೇ",
        6: "ಆರನೇ",
        7: "ಏಳನೇ",
        8: "ಎಂಟನೇ",
        9: "ಒಂಬತ್ತನೇ",
        10: "ಹತ್ತನೇ",
    }
    return names.get(value, f"{value}ನೇ")

def parent_possessive_kn(person):
    return gendered(person, "ತಂದೆಯ", "ತಾಯಿಯ", "ಪೋಷಕರ")

def sibling_label_kn(person, possessive=False):
    if possessive:
        return gendered(person, "ಸಹೋದರನ", "ಸಹೋದರಿಯ", "ಸಹೋದರ/ಸಹೋದರಿಯ")
    return gendered(person, "ಸಹೋದರ", "ಸಹೋದರಿ", "ಸಹೋದರ/ಸಹೋದರಿ")

def child_label_kn(person, possessive=False):
    if possessive:
        return gendered(person, "ಮಗನ", "ಮಗಳ", "ಮಗುವಿನ")
    return gendered(person, "ಮಗ", "ಮಗಳು", "ಮಗು")

def ancestor_path_to_id(person, ancestor_id, people_by_id):
    path = []
    current = person
    seen = set()

    while current and current.id not in seen:
        path.append(current)
        if current.id == ancestor_id:
            return path
        seen.add(current.id)
        current = people_by_id.get(current.parent_id) if current.parent_id else None

    return []

def path_relationship_kn(person1, person2, lca_id, people_by_id):
    path1_up = ancestor_path_to_id(person1, lca_id, people_by_id)
    path2_up = ancestor_path_to_id(person2, lca_id, people_by_id)
    if len(path1_up) < 2 or len(path2_up) < 2:
        return ""

    path1_down = list(reversed(path1_up))
    branch1_child = path1_down[1]
    branch2_chain = list(reversed(path2_up))[1:]
    labels = [parent_possessive_kn(person) for person in reversed(branch2_chain)]

    remaining_descendants = path1_down[2:]
    labels.append(sibling_label_kn(branch1_child, possessive=bool(remaining_descendants)))

    for index, descendant in enumerate(remaining_descendants):
        labels.append(child_label_kn(descendant, possessive=index < len(remaining_descendants) - 1))

    return " ".join(labels)

def cousin_label_kn(distance1, distance2, path_label):
    degree = min(distance1, distance2) - 1
    removed = abs(distance1 - distance2)
    label = f"{ordinal_kn(degree)} ಹಂತದ ಸೋದರ ಸಂಬಂಧಿ"
    if removed:
        label = f"{label}, {removed} ತಲೆಮಾರಿನ ಅಂತರ"
    return f"{label} ({path_label})" if path_label else label

def exact_relationship_kn(person1, person2, ancestors1, ancestors2, people_by_id):
    if person1.id == person2.id:
        return "ಅದೇ ವ್ಯಕ್ತಿ"

    if person1.id in ancestors2:
        return ancestor_relation_kn(person1, ancestors2[person1.id])

    if person2.id in ancestors1:
        return descendant_relation_kn(person1, ancestors1[person2.id])

    common_ids = set(ancestors1).intersection(ancestors2)
    if not common_ids:
        return "ನೇರ ಸಂಬಂಧ ಇಲ್ಲ"

    lca_id = min(common_ids, key=lambda person_id: ancestors1[person_id] + ancestors2[person_id])
    distance1 = ancestors1[lca_id]
    distance2 = ancestors2[lca_id]

    if distance1 == 1 and distance2 == 1:
        return gendered(person1, "ಸಹೋದರ", "ಸಹೋದರಿ", "ಸಹೋದರ/ಸಹೋದರಿ")

    if distance1 >= 2 and distance2 >= 2:
        path_label = path_relationship_kn(person1, person2, lca_id, people_by_id)
        return cousin_label_kn(distance1, distance2, path_label)

    path_label = path_relationship_kn(person1, person2, lca_id, people_by_id)
    return path_label

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
            return "ಮರಿ ಮೊಮ್ಮಗ"
        if "granddaughter" in relationship:
            return "ಮರಿ ಮೊಮ್ಮಗಳು"
        if "grandchild" in relationship:
            return "ಮರಿ ಮೊಮ್ಮಗು"
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
