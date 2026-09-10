import json
from app.main import app

openapi_schema = app.openapi()

with open("openapi.json", "w", encoding="utf-8") as f:
    json.dump(openapi_schema, f, indent=2)

print("openapi.json generated successfully. Top keys:", list(openapi_schema.keys()))
print("Total paths defined:", len(openapi_schema.get("paths", {})))
for path in openapi_schema.get("paths", {}):
    print(" -", path)
