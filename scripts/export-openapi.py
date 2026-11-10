"""Write the API's OpenAPI document. Frontend types are generated from this file."""
import json
import os
import sys

os.environ.setdefault("SP_ENV", "test")
from storypanel.api.app import create_app  # noqa: E402

spec = create_app(start_dispatcher=False).openapi()
out = sys.argv[1] if len(sys.argv) > 1 else "packages/api-types/openapi.json"
with open(out, "w") as f:
    json.dump(spec, f, indent=1, sort_keys=True)
    f.write("\n")
print(f"wrote {out}: {len(spec['paths'])} paths")
