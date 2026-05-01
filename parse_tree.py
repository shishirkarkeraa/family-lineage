import xml.etree.ElementTree as ET
import re

tree = ET.parse('/Users/shishirkarkera/Downloads/Family/FamTree2.drawio.xml')
root = tree.getroot()

nodes = {}
edges = []

for cell in root.iter('mxCell'):
    cell_id = cell.get('id')
    value = cell.get('value', '')
    source = cell.get('source')
    target = cell.get('target')
    
    if value:
        clean_val = re.sub(r'<[^>]+>', '', value).strip()
        if clean_val and clean_val != '&nbsp;':
            clean_val = clean_val.replace('&nbsp;', ' ').strip()
            nodes[cell_id] = clean_val
            
    if source and target:
        edges.append((source, target))

print(f"Total valid nodes: {len(nodes)}")
print(f"Total valid edges: {len(edges)}")
print("Sample nodes:")
for i, (k, v) in enumerate(nodes.items()):
    if i < 10: print(f"{k}: {v}")
    
print("Sample edges:")
for e in edges[:10]:
    print(f"{e[0]} -> {e[1]}")
