import io, json, requests
import pandas as pd
from tabulate import tabulate

PROFILE_PATH = "/home/tw_analytics/delta_share/config.share"
TABLE = None   # Set to a table name to query it, or leave None to pick interactively

with open(PROFILE_PATH) as f:
    profile = json.load(f)

ENDPOINT = profile["endpoint"].rstrip("/")
BEARER_TOKEN = profile["bearerToken"]
HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}


def get(path: str) -> dict:
    """Send an authenticated GET request to the Delta Sharing endpoint and return the JSON response."""
    r = requests.get(f"{ENDPOINT}{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def discover_tables() -> list[dict]:
    """Walk all shares → schemas → tables and return a flat list of {share, schema, table} dicts."""
    tables = []
    for share in get("/shares")["items"]:
        for schema in get(f"/shares/{share['name']}/schemas")["items"]:
            for table in get(f"/shares/{share['name']}/schemas/{schema['name']}/tables")["items"]:
                tables.append({"share": share["name"], "schema": schema["name"], "table": table["name"]})
    return tables


def display_metadata(entry: dict) -> None:
    """Fetch and print the table description, query URL, and column schema for the given table entry."""
    share, schema, table = entry["share"], entry["schema"], entry["table"]
    url = f"{ENDPOINT}/shares/{share}/schemas/{schema}/tables/{table}/metadata"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    raw = {}
    for line in r.text.strip().splitlines():
        obj = json.loads(line)
        if "metaData" in obj:
            raw = obj["metaData"]
            break
    description = raw.get("description") or ""
    fields = json.loads(raw.get("schemaString", '{"fields":[]}')).get("fields", [])
    columns = [
        {
            "column": f["name"],
            "type": f["type"] if isinstance(f["type"], str) else f["type"].get("type", ""),
            "nullable": f.get("nullable", True),
            "description": f.get("metadata", {}).get("comment") or "",
        }
        for f in fields
    ]
    header = f"{share}.{schema}.{table}"
    if description:
        header += f"  —  {description}"
    print(f"\ntable_url: {header}")
    print(f"query_url: {ENDPOINT}/shares/{share}/schemas/{schema}/tables/{table}/query")
    if columns:
        print(tabulate(columns, headers="keys", tablefmt="psql", showindex=False))
    else:
        print("  (no column metadata available)")


def query_table(entry: dict) -> None:
    """Query the Delta Sharing table, download the parquet files, and print the result as a table."""
    share, schema, table = entry["share"], entry["schema"], entry["table"]
    query_url = f"{ENDPOINT}/shares/{share}/schemas/{schema}/tables/{table}/query"
    response = requests.post(query_url, headers=HEADERS, json={"limitHint": 50})
    response.raise_for_status()
    lines = [json.loads(line) for line in response.text.strip().splitlines()]
    file_urls = [line["file"]["url"] for line in lines if "file" in line]
    frames = [pd.read_parquet(io.BytesIO(requests.get(url).content)) for url in file_urls]
    df = pd.concat(frames, ignore_index=True)
    print("\nData:\n")
    print(tabulate(df, headers="keys", tablefmt="psql", showindex=False))


def main() -> None:
    """Discover available tables, prompt the user to pick one if ambiguous, then display metadata and query it."""
    all_tables = discover_tables()
    matches = all_tables if TABLE is None else [t for t in all_tables if t["table"] == TABLE]

    if not matches:
        raise ValueError(f"Table '{TABLE}' not found. Set TABLE=None to list all tables.")

    if len(matches) == 1:
        chosen = matches[0]
    else:
        label = "Available tables" if TABLE is None else f"Multiple tables named '{TABLE}'"
        print(f"{label}:\n")
        rows = [{"#": i + 1, **t} for i, t in enumerate(matches)]
        print(tabulate(rows, headers="keys", tablefmt="psql", showindex=False))
        for entry in matches:
            display_metadata(entry)
        choice = input(f"\nEnter number (1-{len(matches)}) to query, or press Enter to exit: ").strip()
        if not choice:
            return
        idx = int(choice) - 1
        if not (0 <= idx < len(matches)):
            raise ValueError(f"Invalid selection: {choice}")
        chosen = matches[idx]

    display_metadata(chosen)
    query_table(chosen)


if __name__ == "__main__":
    main()
