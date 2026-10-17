import json
from pathlib import Path

txt = Path("web/data/flows.js").read_text(encoding="utf-8")
d = json.loads(txt[txt.index("{") : txt.rstrip().rindex("}") + 1])
names = {n["id"]: n["name"] for n in d["nodes"]}
cred = sorted(
    [(l["v"], names[l["t"]]) for l in d["links"] if l["s"] == "CREDIT"], reverse=True
)
sav = sorted(
    [(l["v"], names[l["s"]]) for l in d["links"] if l["t"] == "SAVING"], reverse=True
)
print("CREDIT ->", sum(v for v, _ in cred))
for v, n in cred[:15]:
    print(f"  {v:8.1f} {n}")
print("-> SAVING", sum(v for v, _ in sav))
for v, n in sav[:15]:
    print(f"  {v:8.1f} {n}")
for n in d["nodes"]:
    if n["id"].startswith("HH") or n["id"] in ("SL", "FRB"):
        print(
            f"{n['name']:50s} in {n['inflow']:8.1f} out {n['outflow']:8.1f} dwell {n['dwell']:7.1f}"
        )
