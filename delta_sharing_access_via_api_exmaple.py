"""
Delta Sharing REST API test client (open sharing / bearer token).


"""

import io
import json
import sys
from datetime import datetime, timezone

import pandas as pd
import requests
from tabulate import tabulate

# ── Config ────────────────────────────────────────────────────────────────────
PROFILE_PATH = "/home/tw_analytics/delta_share/config.share"

# Hardcode your target here, or leave as None to use the first table discovered.
SHARE_NAME: str | None = None   # e.g. "external_test"
SCHEMA_NAME: str | None = None  # e.g. "gold"
TABLE_NAME: str | None = None   # e.g. "my_view"

# Set True to ignore HTTP(S)_PROXY env vars / system proxy. Useful to test
# whether a corporate proxy is mangling the pre-signed URL signature.
BYPASS_PROXY = False

# Views/materialized assets may default to delta response format; force parquet
# so the simple {"file": {"url": ...}} parsing below is valid.
CAPABILITIES = {"delta-sharing-capabilities": "responseformat=parquet"}

# ── Load credentials from profile ─────────────────────────────────────────────
with open(PROFILE_PATH) as f:
    profile = json.load(f)

ENDPOINT = profile["endpoint"].rstrip("/")
AUTH_HEADERS = {"Authorization": f"Bearer {profile['bearerToken']}"}

# Session for the Delta Sharing server (carries the bearer token per-call).
api = requests.Session()
# Session for storage downloads: NO default headers, ever.
storage = requests.Session()

if BYPASS_PROXY:
    api.trust_env = False
    storage.trust_env = False


def api_get(path: str, extra_headers: dict | None = None) -> requests.Response:
    r = api.get(
        f"{ENDPOINT}{path}",
        headers={**AUTH_HEADERS, **(extra_headers or {})},
        timeout=60,
    )
    r.raise_for_status()
    return r


def parse_ndjson(text: str) -> list[dict]:
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


# ── Discover: list shares -> schemas -> tables ────────────────────────────────
all_tables: list[tuple[str, str, str]] = []
shares = api_get("/shares").json().get("items", [])
print("Shares:", [s["name"] for s in shares])

for s in shares:
    for sc in api_get(f"/shares/{s['name']}/schemas").json().get("items", []):
        tables = api_get(
            f"/shares/{s['name']}/schemas/{sc['name']}/tables"
        ).json().get("items", [])
        for t in tables:
            print(f"  {s['name']}.{sc['name']}.{t['name']}")
            all_tables.append((s["name"], sc["name"], t["name"]))

if not all_tables:
    sys.exit("No tables found in any share. Check grants on the provider side.")

# Resolve target table
if SHARE_NAME and SCHEMA_NAME and TABLE_NAME:
    target = (SHARE_NAME, SCHEMA_NAME, TABLE_NAME)
    if target not in all_tables:
        sys.exit(f"Configured table {'.'.join(target)} not found in discovered tables.")
else:
    target = all_tables[0]
    print(f"\nNo table hardcoded - using first discovered: {'.'.join(target)}")

share_name, schema_name, table_name = target
base = f"/shares/{share_name}/schemas/{schema_name}/tables/{table_name}"

# ── Metadata ──────────────────────────────────────────────────────────────────
meta_lines = parse_ndjson(api_get(f"{base}/metadata", CAPABILITIES).text)
protocol = next((l["protocol"] for l in meta_lines if "protocol" in l), {})
meta = next((l["metaData"] for l in meta_lines if "metaData" in l), {})
table_schema = json.loads(meta.get("schemaString", "{}"))
columns = table_schema.get("fields", [])

print("\n── Table Metadata ───────────────────────────────────────────")
print(f"  Table       : {share_name}.{schema_name}.{table_name}")
print(f"  ID          : {meta.get('id', 'N/A')}")
print(f"  Format      : {meta.get('format', {}).get('provider', 'N/A')}")
print(f"  Protocol    : minReaderVersion={protocol.get('minReaderVersion', 'N/A')}")
print(f"  Partitioned : {meta.get('partitionColumns') or 'None'}")
print(f"\n── Columns ({len(columns)}) ─────────────────────────────────────────")
col_w = max((len(c["name"]) for c in columns), default=10)
print(f"  {'Column':<{col_w}}  {'Type':<30}  Nullable")
print(f"  {'-' * col_w}  {'-' * 30}  --------")
for col in columns:
    print(f"  {col['name']:<{col_w}}  {str(col['type']):<30}  {col.get('nullable', True)}")
print("─────────────────────────────────────────────────────────────\n")

# ── Query: get fresh pre-signed file URLs ─────────────────────────────────────
query_url = f"{ENDPOINT}{base}/query"
print("Query URL:", query_url)

resp = api.post(
    query_url,
    headers={**AUTH_HEADERS, **CAPABILITIES},
    json={"limitHint": 50},
    timeout=120,
)
resp.raise_for_status()
lines = parse_ndjson(resp.text)

files = [l["file"] for l in lines if "file" in l]
if not files:
    sys.exit(
        "Query returned no file actions. If this is a shared view, the "
        "materialization may have failed on the provider side - check the "
        "response above and the provider's storage/NCC configuration.\n"
        f"Raw response:\n{resp.text[:2000]}"
    )

print(f"Query returned {len(files)} data file(s).")

# ── Download data files IMMEDIATELY (SAS URLs are short-lived) ────────────────
frames = []
for i, f_action in enumerate(files, start=1):
    url = f_action["url"]

    # Sanity checks: catch mangled / rewritten URLs (e.g. Outlook SafeLinks)
    host = url.split("/")[2]
    VALID_HOSTS = ("core.windows.net", "storage.azuredatabricks.net")
    if not any(host.endswith(h) for h in VALID_HOSTS):
        sys.exit(
            f"File URL host looks rewritten/mangled: {host}\n"
            "Pre-signed URLs must point directly at *.core.windows.net or "
            "*.storage.azuredatabricks.net. "
            "Something (proxy, SafeLinks, copy-paste) altered the URL."
        )

    exp_ms = f_action.get("expirationTimestamp")
    if exp_ms:
        exp = datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc)
        remaining = (exp - datetime.now(timezone.utc)).total_seconds()
        print(f"  file {i}: SAS expires {exp.isoformat()} ({remaining:.0f}s from now)")
        if remaining <= 0:
            sys.exit("  -> SAS already expired. Check your system clock (NTP sync).")

    # CRITICAL: no Authorization header here - the SAS in the URL is the auth.
    data = storage.get(url, timeout=120)
    if data.status_code != 200:
        print(f"\nStorage download failed: HTTP {data.status_code}")
        print("Azure error body (contains the real error code):")
        print(data.text[:2000])
        print(
            "\nHints:\n"
            "  AuthenticationFailed  -> bad/altered/expired SAS, clock skew, or an\n"
            "                           Authorization header was sent (must not be).\n"
            "  AuthorizationFailure  -> storage firewall blocking your public IP.\n"
            "  PublicAccessNotPermitted -> storage public network access is Disabled."
        )
        data.raise_for_status()

    frames.append(pd.read_parquet(io.BytesIO(data.content)))

df = pd.concat(frames, ignore_index=True)
print(f"\nLoaded {len(df)} rows x {len(df.columns)} columns")
print(tabulate(df.head(50), headers="keys", tablefmt="psql", showindex=False))
