import os
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from pyzotero import zotero

ZOTERO_API_KEY = os.environ["ZOTERO_API_KEY"]
ZOTERO_LIBRARY_ID = os.environ["ZOTERO_LIBRARY_ID"]
ZOTERO_LIBRARY_TYPE = os.environ.get("ZOTERO_LIBRARY_TYPE", "user")

mcp = FastMCP("ZoteroMCP")


def get_zotero():
    return zotero.Zotero(ZOTERO_LIBRARY_ID, ZOTERO_LIBRARY_TYPE, ZOTERO_API_KEY)


def format_item(item: dict) -> str:
    data = item["data"]
    key = data.get("key", item.get("key", ""))
    title = data.get("title", "No title")
    item_type = data.get("itemType", "unknown")

    authors = []
    for c in data.get("creators", []):
        if c.get("creatorType") == "author":
            name = c.get("lastName", "")
            if c.get("firstName"):
                name += f", {c['firstName']}"
            authors.append(name)

    date = data.get("date", "")
    year = date[:4] if date else ""
    venue = data.get("publicationTitle") or data.get("conferenceName") or data.get("bookTitle", "")
    abstract = data.get("abstractNote", "")

    parts = [f"Key: {key}", f"Title: {title}", f"Type: {item_type}"]
    if authors:
        parts.append(f"Authors: {', '.join(authors)}")
    if year:
        parts.append(f"Year: {year}")
    if venue:
        parts.append(f"Venue: {venue}")
    if abstract:
        snippet = abstract[:500] + ("..." if len(abstract) > 500 else "")
        parts.append(f"Abstract: {snippet}")

    return "\n".join(parts)


@mcp.tool()
def search_papers(query: str, limit: int = 10) -> str:
    """Search for papers in the Zotero library by keywords. Returns title, authors, year, venue, and abstract."""
    zot = get_zotero()
    items = zot.items(q=query, limit=limit)

    results = [
        format_item(item)
        for item in items
        if item["data"].get("itemType") not in ("attachment", "note")
    ]

    if not results:
        return f"No papers found for: {query}"

    return f"Found {len(results)} paper(s):\n\n" + "\n\n---\n\n".join(results)


@mcp.tool()
def get_paper_fulltext(item_key: str) -> str:
    """Get the full indexed text of a paper by its Zotero item key. Use search_papers first to get keys."""
    zot = get_zotero()

    # Look for PDF attachments on the parent item
    try:
        children = zot.children(item_key)
        for child in children:
            if child["data"].get("contentType") == "application/pdf":
                result = zot.fulltext_item(child["key"])
                content = result.get("content", "")
                if content:
                    indexed = result.get("indexedPages", "?")
                    total = result.get("totalPages", "?")
                    return f"Full text ({indexed}/{total} pages indexed):\n\n{content}"
    except Exception:
        pass

    # Fall back in case item_key is itself an attachment
    try:
        result = zot.fulltext_item(item_key)
        content = result.get("content", "")
        if content:
            return f"Full text:\n\n{content}"
    except Exception:
        pass

    return "No full text available. The PDF may not be indexed or synced to Zotero."


@mcp.tool()
def get_paper_metadata(item_key: str) -> str:
    """Get complete metadata for a specific paper by its Zotero item key."""
    zot = get_zotero()
    item = zot.item(item_key)
    data = item["data"]

    skip = {"key", "version", "collections", "relations"}
    lines = [f"{k}: {v}" for k, v in data.items() if k not in skip and v]

    tags = [t["tag"] for t in data.get("tags", [])]
    if tags:
        lines.append(f"tags: {', '.join(tags)}")

    collections = data.get("collections", [])
    if collections:
        lines.append(f"collections: {', '.join(collections)}")

    return "\n".join(lines)


@mcp.tool()
def list_collections() -> str:
    """List all collections in the Zotero library with their names and keys."""
    zot = get_zotero()
    collections = zot.collections()

    if not collections:
        return "No collections found."

    lines = []
    for col in collections:
        data = col["data"]
        key = data.get("key", col.get("key", ""))
        name = data.get("name", "Unnamed")
        count = col.get("meta", {}).get("numItems", "?")
        lines.append(f"Key: {key} | Name: {name} | Items: {count}")

    return f"{len(collections)} collection(s):\n\n" + "\n".join(lines)


@mcp.tool()
def get_collection_papers(collection_key: str, limit: int = 20) -> str:
    """Get papers in a specific Zotero collection. Use list_collections to find collection keys."""
    zot = get_zotero()
    items = zot.collection_items(collection_key, limit=limit)

    results = [
        format_item(item)
        for item in items
        if item["data"].get("itemType") not in ("attachment", "note")
    ]

    if not results:
        return "No papers found in this collection."

    return f"Found {len(results)} paper(s):\n\n" + "\n\n---\n\n".join(results)


# SSE transport for Railway deployment
sse_transport = SseServerTransport("/messages/")


async def handle_sse(request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp._mcp_server.run(
            streams[0], streams[1],
            mcp._mcp_server.create_initialization_options(),
        )


app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse),
    Mount("/messages/", app=sse_transport.handle_post_message),
])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
