#!/usr/bin/env python3
"""
seed_anniversary.py — Run ONCE after creating the two Airtable tables.

Usage:
  ANNIVERSARY_AWARDS_TABLE_ID=tblXXX ANNIVERSARY_PARTICIPANTS_TABLE_ID=tblYYY python3 seed_anniversary.py

Or set the vars at the top of this file.
"""
import os, json, time, requests

BASE_ID     = "app3xt0dghBWnHxdN"
# The token comes from the environment only (same Railway var app.py uses).
# A live PAT used to sit here as the default and was committed to GitHub
# (found in the 2026-10-22 Phase 0 audit); that token has to be revoked in
# Airtable and never put back in this file.
WRITE_TOKEN = os.environ.get("SO_TRACKING_WRITE_TOKEN", "")

AWARDS_TABLE       = os.environ.get("ANNIVERSARY_AWARDS_TABLE_ID", "")
PARTICIPANTS_TABLE = os.environ.get("ANNIVERSARY_PARTICIPANTS_TABLE_ID", "")

if not WRITE_TOKEN:
    print("ERROR: Set SO_TRACKING_WRITE_TOKEN (an Airtable PAT with write access to the Automated Data base).")
    exit(1)
if not AWARDS_TABLE or not PARTICIPANTS_TABLE:
    print("ERROR: Set ANNIVERSARY_AWARDS_TABLE_ID and ANNIVERSARY_PARTICIPANTS_TABLE_ID env vars.")
    exit(1)

HEADERS = {
    "Authorization": f"Bearer {WRITE_TOKEN}",
    "Content-Type": "application/json",
}

# ── Awards ─────────────────────────────────────────────────────────────────────
AWARDS = [
    # (name, points, category)
    ("Starbucks Gift Card",   1,  "Gift Card"),
    ("Chick-fil-A Gift Card", 1,  "Gift Card"),
    ("Target Gift Card",      1,  "Gift Card"),
    ("Mixing Bowls",          5,  "Kitchen"),
    ("Towel",                 5,  "Home"),
    ("Echo Dot",              5,  "Tech"),
    ("Neck Massager",         5,  "Wellness"),
    ("Knife",                 5,  "Kitchen"),
    ("Hammock",               10, "Outdoor"),
    ("Ice Cream Maker",       10, "Kitchen"),
    ("JBL Speaker",           10, "Tech"),
    ("Ring Doorbell",         10, "Home"),
    ("Shop Vac",              10, "Tools"),
    ("Drill",                 15, "Tools"),
    ("Telescope",             15, "Outdoor"),
    ("Suitcase",              15, "Travel"),
    ("Kindle",                15, "Tech"),
    ("Shade Tent",            15, "Outdoor"),
    ("AirPods",               20, "Tech"),
    ("Ninja Foodi",           20, "Kitchen"),
    ("RTIC Cooler",           20, "Outdoor"),
    ("Roomba",                20, "Home"),
    ("TV",                    20, "Tech"),
    ("Apple Watch",           25, "Tech"),
    ("Smoker",                25, "Outdoor"),
    ("Nintendo Switch",       25, "Tech"),
    ("Knife Set",             25, "Kitchen"),
    ("Echo Show",             25, "Tech"),
]

print(f"Seeding {len(AWARDS)} awards into {AWARDS_TABLE}…")
for i in range(0, len(AWARDS), 10):
    batch = AWARDS[i:i+10]
    records = [{"fields": {"Name": n, "Points": p, "Category": c, "Active": True}}
               for n, p, c in batch]
    r = requests.post(
        f"https://api.airtable.com/v0/{BASE_ID}/{AWARDS_TABLE}",
        headers=HEADERS,
        json={"records": records},
        timeout=15,
    )
    if r.status_code not in (200, 201):
        print(f"  ERROR on batch {i}: {r.status_code} {r.text[:200]}")
    else:
        created = r.json().get("records", [])
        for rec in created:
            print(f"  ✓ {rec['fields']['Name']} ({rec['fields']['Points']}pt) → {rec['id']}")
    time.sleep(0.3)

# ── Participants ───────────────────────────────────────────────────────────────
PARTICIPANTS = [
    # Pre-generated tokens — do NOT regenerate or links will break
    {"first": "Zachary",   "last": "Poole",       "points": 1},
    {"first": "Saylor",    "last": "Clough",       "points": 1},
    {"first": "Aaron",     "last": "Stock",        "points": 1},
    {"first": "Nathan",    "last": "Poole",        "points": 1},
    {"first": "Hunter",    "last": "Zysk",         "points": 1},
    {"first": "Yoli",      "last": "Clark",        "points": 1},
    {"first": "Enzlie",    "last": "Drewery",      "points": 1},
    {"first": "Becca",     "last": "Olmstead",     "points": 1},
    {"first": "Lily",      "last": "Holloway",     "points": 1},
    {"first": "Lensey",    "last": "Putnam",       "points": 1},
    {"first": "Tamzin",    "last": "Collett",      "points": 1},
    {"first": "Zaden",     "last": "Jessup",       "points": 1},
    {"first": "Ian",       "last": "Nash",         "points": 1},
    {"first": "Lila",      "last": "McGee",        "points": 1},
    {"first": "Rafe",      "last": "Sipes",        "points": 1},
    {"first": "Cache",     "last": "Bartholomew",  "points": 1},
    {"first": "Jocelyn",   "last": "McGee",        "points": 1},
    {"first": "Sophia",    "last": "Burford",      "points": 5},
    {"first": "Mike",      "last": "Newton",       "points": 5},
    {"first": "Riley",     "last": "Ratzlaff",     "points": 5},
    {"first": "Samuel",    "last": "Quinn",        "points": 5},
    {"first": "Ameerah",   "last": "Stampley",     "points": 5},
    {"first": "Lucy",      "last": "Tilson",       "points": 5},
    {"first": "Ashley",    "last": "Whipkey",      "points": 5},
    {"first": "Jaimee",    "last": "Huddleston",   "points": 5},
    {"first": "Maya",      "last": "Alba",         "points": 5},
    {"first": "Katie",     "last": "Sipes",        "points": 5},
    {"first": "Bailey",    "last": "Hanner",       "points": 5},
    {"first": "Brittney",  "last": "Jones",        "points": 5},
    {"first": "Addison",   "last": "Hanner",       "points": 5},
    {"first": "Christine", "last": "Young",        "points": 10},
    {"first": "Kennedy",   "last": "Gramme",       "points": 10},
    {"first": "Azure",     "last": "Collett",      "points": 10},
    {"first": "Elias",     "last": "Gause",        "points": 10},
    {"first": "Marla",     "last": "Bartholomew",  "points": 10},
    {"first": "Melissa",   "last": "Kuehl-Coe",    "points": 10},
    {"first": "Aricka",    "last": "Drewery",      "points": 10},
    {"first": "Brighton",  "last": "Apostolo",     "points": 10},
    {"first": "Rachel",    "last": "Whipkey",      "points": 10},
    {"first": "Ben",       "last": "Krohn",        "points": 10},
    {"first": "Sean",      "last": "Scott",        "points": 10},
    {"first": "Tyler",     "last": "Jackson",      "points": 10},
    {"first": "Natalie",   "last": "Archer",       "points": 10},
    {"first": "Josie",     "last": "Exner",        "points": 10},
    {"first": "Chris",     "last": "Cooke",        "points": 15},
    {"first": "Gaby",      "last": "Anderson",     "points": 15},
    {"first": "Margaret",  "last": "Hennum",       "points": 15},
    {"first": "Andrew",    "last": "Turner",       "points": 15},
    {"first": "Phov",      "last": "Nix",          "points": 15},
    {"first": "Traci",     "last": "Houze",        "points": 15},
    {"first": "Stephen",   "last": "Sargent",      "points": 20},
    {"first": "Carol",     "last": "Poole",        "points": 20},
    {"first": "Amy",       "last": "Mitchell",     "points": 20},
    {"first": "Michelle",  "last": "Tandy",        "points": 20},
    {"first": "Carissa",   "last": "Crooks",       "points": 20},
    {"first": "Karen",     "last": "Scoville",     "points": 25},
    {"first": "Lisa",      "last": "Barnes",       "points": 25},
    {"first": "Wendy",     "last": "Alba",         "points": 25},
    {"first": "Michelle",  "last": "Stampley",     "points": 25},
    {"first": "Ethel",     "last": "Bolton",       "points": 25},
    {"first": "Joni",      "last": "Apostolo",     "points": 25},
]

# Load pre-generated tokens
TOKEN_FILE = "/tmp/tokens.json"
try:
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    # Build lookup: (first.lower(), last.lower()) → token
    token_map = {(t["first"].lower(), t["last"].lower()): t["token"] for t in token_data}
    print(f"Loaded {len(token_map)} tokens from {TOKEN_FILE}")
except Exception as e:
    print(f"WARNING: Could not load tokens from {TOKEN_FILE}: {e}")
    print("Generating new tokens (links will differ from any previously shared)…")
    import uuid
    token_map = {}
    for p in PARTICIPANTS:
        token_map[(p["first"].lower(), p["last"].lower())] = str(uuid.uuid4())

print(f"\nSeeding {len(PARTICIPANTS)} participants into {PARTICIPANTS_TABLE}…")
for i in range(0, len(PARTICIPANTS), 10):
    batch = PARTICIPANTS[i:i+10]
    records = []
    for p in batch:
        tok = token_map.get((p["first"].lower(), p["last"].lower()), "")
        records.append({"fields": {
            "First Name": p["first"],
            "Last Name":  p["last"],
            "Points":     p["points"],
            "Token":      tok,
            "Submitted":  False,
        }})
    r = requests.post(
        f"https://api.airtable.com/v0/{BASE_ID}/{PARTICIPANTS_TABLE}",
        headers=HEADERS,
        json={"records": records},
        timeout=15,
    )
    if r.status_code not in (200, 201):
        print(f"  ERROR on batch {i}: {r.status_code} {r.text[:200]}")
    else:
        created = r.json().get("records", [])
        for rec in created:
            f = rec["fields"]
            print(f"  ✓ {f['First Name']} {f['Last Name']} ({f['Points']}pt) → {rec['id']}")
    time.sleep(0.3)

print("\nDone! Now set ANNIVERSARY_AWARDS_TABLE_ID and ANNIVERSARY_PARTICIPANTS_TABLE_ID in Railway.")
