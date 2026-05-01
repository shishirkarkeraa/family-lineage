import xml.etree.ElementTree as ET
import re
from database import SessionLocal, init_db, Person, Base, engine
from indic_transliteration import sanscript
import unicodedata

def clean_name(value):
    clean_val = re.sub(r'<[^>]+>', '', value).strip()
    if clean_val and clean_val != '&nbsp;':
        clean_val = clean_val.replace('&nbsp;', ' ').strip()
        return clean_val
    return None

def to_english(kannada_text):
    res = sanscript.transliterate(kannada_text, sanscript.KANNADA, sanscript.IAST)
    res = ''.join(c for c in unicodedata.normalize('NFD', res) if unicodedata.category(c) != 'Mn')
    res = ' '.join(word.capitalize() for word in res.split())
    return res

def parse_xml_and_seed(xml_file):
    Base.metadata.drop_all(bind=engine)
    init_db()
    
    db = SessionLocal()

    tree = ET.parse(xml_file)
    root = tree.getroot()

    people = {}
    edges = []
    all_nodes = set()

    for cell in root.iter('mxCell'):
        cell_id = cell.get('id')
        value = cell.get('value', '')
        source = cell.get('source')
        target = cell.get('target')
        
        if cell_id:
            all_nodes.add(cell_id)

        if value:
            name_kn = clean_name(value)
            if name_kn:
                name_en = to_english(name_kn)
                people[cell_id] = {'name_kn': name_kn, 'name_en': name_en}
                
        if source and target:
            edges.append((source, target))

    print(f"Found {len(people)} people.")
    
    adj = {n: [] for n in all_nodes}
    rev_adj = {n: [] for n in all_nodes}
    
    for s, t in edges:
        adj[s].append(t)
        rev_adj[t].append(s)

    parent_map = {}
    
    for person_id in people:
        curr = person_id
        parent_id = None
        visited = set()
        
        while curr in rev_adj and rev_adj[curr]:
            p = rev_adj[curr][0]
            if p in visited:
                break
            visited.add(p)
            if p in people:
                parent_id = p
                break
            curr = p
            
        parent_map[person_id] = parent_id

    db_persons = {}
    for pid, names in people.items():
        person = Person(name_kn=names['name_kn'], name_en=names['name_en'])
        db.add(person)
        db_persons[pid] = person
        
    db.commit()

    for pid, person in db_persons.items():
        if parent_map.get(pid):
            parent_pid = parent_map[pid]
            if parent_pid in db_persons:
                person.parent_id = db_persons[parent_pid].id
                
    db.commit()
    print("Database seeding completed.")
    db.close()

if __name__ == "__main__":
    parse_xml_and_seed('/Users/shishirkarkera/Downloads/Family/FamTree2.drawio.xml')
