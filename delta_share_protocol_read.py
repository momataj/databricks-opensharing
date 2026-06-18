"""
Delta Sharing protocol read template (delta-sharing library).

Connects to a Delta Share provider using the official ``delta-sharing`` Python
library and discovers all shared tables the configured recipient has access to.
For each table it prints the share/schema/table coordinates, loads the full
table into a pandas DataFrame, and displays the row count, column schema, and
a preview of the data.

Authentication
--------------
Reads credentials from a Delta Share profile file (JSON) at PROFILE_PATH.
The profile must contain ``shareCredentialsVersion``, ``endpoint``, and
``bearerToken`` fields as issued by the provider.

Profile path
------------
Default: ``/home/tw_analytics/delta_share/config.share``
Change PROFILE_PATH below to point to a different profile file.

Notes
-----
- ``delta_sharing.load_as_pandas`` fetches the entire table; use
  ``limit=`` or ``jsonPredicateHints`` for large tables.
- For a lower-level REST-based approach (no library dependency) see
  ``templates/delta_sharing_access_via_api_exmaple.py``.
"""

import json

import delta_sharing
import pandas as pd

# Point to the profile file. It can be a file on the local file system or a file on a remote storage.
PROFILE_PATH = "/home/tw_analytics/delta_share/config.share"

# Create a SharingClient.
client = delta_sharing.SharingClient(PROFILE_PATH)

# List all shared tables and load each one dynamically.
shares = client.list_shares()

for share in shares:
    schemas = client.list_schemas(share)
    for schema in schemas:
        tables = client.list_tables(schema)
        for table in tables:
            print(f'name = {table.name}, share = {table.share}, schema = {table.schema}')

            table_url = f"{PROFILE_PATH}#{table.share}.{table.schema}.{table.name}"

            # ── Metadata ─────────────────────────────────────────────────────
            protocol = delta_sharing.get_table_protocol(table_url)
            metadata = delta_sharing.get_table_metadata(table_url)
            fields = json.loads(metadata.schema_string).get("fields", [])
            col_w = max((len(c["name"]) for c in fields), default=10)

            print(f'  id          : {metadata.id}')
            print(f'  description : {metadata.description or "N/A"}')
            print(f'  format      : {metadata.format.provider}')
            print(f'  protocol    : minReaderVersion={protocol.min_reader_version}')
            print(f'  partitioned : {metadata.partition_columns or "None"}')
            print(f'  columns ({len(fields)}):')
            for col in fields:
                comment = col.get("metadata", {}).get("comment", "")
                print(f'    {col["name"]:<{col_w}}  {str(col["type"]):<30}  nullable={col.get("nullable", True)}  {comment}')
            print()
            # ─────────────────────────────────────────────────────────────────
            print(f"table_url:{table_url}")
            pandas_df = delta_sharing.load_as_pandas(table_url, limit=50)

            print(f'  row count = {len(pandas_df)}')
            print(f'  columns:')
            for col, dtype in pandas_df.dtypes.items():
                print(f'    {col}: {dtype}')
            print(pandas_df)
