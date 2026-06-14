"""
Anthropic tool schemas that mirror the MCP server's 4 tools.
dispatch_tool is async — write tools route through the shared aiosqlite connection.
"""
from mcp_server.server import (
    read_sql,
    async_create_purchase_request,
    async_get_purchase_status,
    async_complete_purchase,
)

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "read_sql",
        "description": (
            "Execute a read-only SELECT query against the Chinook music catalog database. "
            "Use this to look up tracks, albums, artists, genres, playlists, and past invoices. "
            "Only SELECT or WITH (CTE) statements are permitted. "
            "Always use LIMIT on large tables (Track has 3503 rows). "
            "Returns rows as a JSON array of objects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A valid SQLite SELECT query.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_purchase_request",
        "description": (
            "Submit a purchase request for a list of tracks. "
            "The server validates all TrackIds and computes the total from the catalog price — "
            "never compute or guess prices yourself. "
            "Returns a purchase_request_id and the server-computed total. "
            "The request goes to admin review; you cannot approve it yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "integer",
                    "description": "The customer's CustomerId (provided in your system prompt).",
                },
                "track_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of TrackIds to purchase. Must be non-empty.",
                },
            },
            "required": ["customer_id", "track_ids"],
        },
    },
    {
        "name": "get_purchase_status",
        "description": (
            "Check the status of a purchase request: pending, approved, denied, or completed. "
            "Call this when the customer asks for an update on their order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purchase_request_id": {
                    "type": "integer",
                    "description": "The purchase_request_id returned by create_purchase_request.",
                }
            },
            "required": ["purchase_request_id"],
        },
    },
    {
        "name": "complete_purchase",
        "description": (
            "Finalise an approved purchase by providing billing/shipping details. "
            "Call this ONLY after get_purchase_status returns status='approved' AND "
            "the customer has explicitly provided their billing address. "
            "Creates the Invoice and InvoiceLine records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purchase_request_id": {"type": "integer"},
                "billing_address": {"type": "string"},
                "billing_city": {"type": "string"},
                "billing_state": {"type": "string", "description": "State or province. Use empty string if not applicable."},
                "billing_country": {"type": "string"},
                "billing_postal_code": {"type": "string", "description": "Postal or ZIP code. Use empty string if not applicable."},
            },
            "required": [
                "purchase_request_id",
                "billing_address",
                "billing_city",
                "billing_state",
                "billing_country",
                "billing_postal_code",
            ],
        },
    },
]


async def dispatch_tool(name: str, tool_input: dict, db=None) -> str:
    """Call the appropriate MCP tool and return a JSON-encoded result string.
    Read-only tools (read_sql) use their own readonly connection.
    Write tools (create_purchase_request, complete_purchase) use the shared db
    to avoid 'database is locked' from two concurrent writers.
    """
    import json

    if name == "read_sql":
        result = read_sql(tool_input["query"])

    elif name == "create_purchase_request":
        result = await async_create_purchase_request(
            db,
            tool_input["customer_id"],
            tool_input["track_ids"],
        )

    elif name == "get_purchase_status":
        result = await async_get_purchase_status(db, tool_input["purchase_request_id"])

    elif name == "complete_purchase":
        result = await async_complete_purchase(
            db,
            tool_input["purchase_request_id"],
            tool_input["billing_address"],
            tool_input["billing_city"],
            tool_input["billing_state"],
            tool_input["billing_country"],
            tool_input["billing_postal_code"],
        )

    else:
        result = {"error": f"Unknown tool: {name}"}

    return json.dumps(result)
