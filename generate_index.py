import os
import json

DATA_DIR = "data/matchups"
OUTPUT_FILE = "data/matchup_index.json"

index = {}
total = 0

for role in os.listdir(DATA_DIR):
    role_dir = os.path.join(DATA_DIR, role)
    if not os.path.isdir(role_dir):
        continue
    keys = []
    for f in sorted(os.listdir(role_dir)):
        if f.endswith(".json"):
            keys.append(f.replace(".json", ""))
    index[role] = keys
    total += len(keys)
    print(f"  {role}: {len(keys)} matchups")

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(index, f)

print(f"\n Done: {OUTPUT_FILE} ({total} matchups)")
