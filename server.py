import json
import os
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from pydantic import BaseModel, Field
from pyzotero import zotero

import pdf_highlight as ph

ZOTERO_API_KEY = os.environ["ZOTERO_API_KEY"]
ZOTERO_LIBRARY_ID = os.environ["ZOTERO_LIBRARY_ID"]
ZOTERO_LIBRARY_TYPE = os.environ.get("ZOTERO_LIBRARY_TYPE", "user")

# FastMCP assumes a localhost-only server and rejects any other Host header on
# /mcp (421). This server is public and token-protected, so turn that check off.
mcp = FastMCP(
    "ZoteroMCP",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


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
        if item["data"].get("itemType") not in ("attachment", "note", "annotation")
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
        if item["data"].get("itemType") not in ("attachment", "note", "annotation")
    ]

    if not results:
        return "No papers found in this collection."

    return f"Found {len(results)} paper(s):\n\n" + "\n\n---\n\n".join(results)


@mcp.tool()
def get_paper_notes(item_key: str) -> str:
    """Get all notes attached to a paper by its Zotero item key. Returns each note's key and HTML content."""
    zot = get_zotero()
    children = zot.children(item_key)

    notes = [c for c in children if c["data"].get("itemType") == "note"]
    if not notes:
        return "No notes found for this item."

    blocks = []
    for n in notes:
        data = n["data"]
        key = data.get("key", n.get("key", ""))
        content = data.get("note", "")
        tags = [t["tag"] for t in data.get("tags", [])]
        parts = [f"Note key: {key}"]
        if tags:
            parts.append(f"Tags: {', '.join(tags)}")
        parts.append(f"Content (HTML):\n{content}")
        blocks.append("\n".join(parts))

    return f"Found {len(notes)} note(s):\n\n" + "\n\n---\n\n".join(blocks)


def format_annotation(ann: dict) -> str:
    data = ann["data"]
    key = data.get("key", ann.get("key", ""))
    ann_type = data.get("annotationType", "unknown")
    page = data.get("annotationPageLabel", "")
    color = data.get("annotationColor", "")
    text = data.get("annotationText", "")
    comment = data.get("annotationComment", "")
    tags = [t["tag"] for t in data.get("tags", [])]

    header = f"[{ann_type}]"
    if page:
        header += f" p. {page}"
    if color:
        header += f" ({color})"
    parts = [f"{header} | Annotation key: {key}"]
    if text:
        parts.append(f"Text: {text}")
    if comment:
        parts.append(f"Comment: {comment}")
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    if ann_type in ("image", "ink") and not text and not comment:
        parts.append("(Image/ink annotation; the drawn content is not available via the API.)")
    return "\n".join(parts)


@mcp.tool()
def get_paper_annotations(item_key: str) -> str:
    """Get annotations (highlights, underlines, comments, sticky notes) made in Zotero's built-in reader for a paper.
    Accepts a paper's item key or an attachment key. Annotations are returned in reading order, grouped per attachment."""
    zot = get_zotero()

    item = zot.item(item_key)
    if item["data"].get("itemType") == "attachment":
        attachments = [item]
    else:
        attachments = [c for c in zot.children(item_key) if c["data"].get("itemType") == "attachment"]

    if not attachments:
        return "No attachments found for this item, so there are no annotations."

    blocks = []
    total = 0
    for att in attachments:
        anns = [
            c for c in zot.everything(zot.children(att["key"]))
            if c["data"].get("itemType") == "annotation"
        ]
        if not anns:
            continue
        anns.sort(key=lambda a: a["data"].get("annotationSortIndex", ""))
        total += len(anns)
        title = att["data"].get("title") or att["data"].get("filename") or att["key"]
        section = f"Attachment: {title} (key: {att['key']}) — {len(anns)} annotation(s)\n\n"
        section += "\n\n".join(format_annotation(a) for a in anns)
        blocks.append(section)

    if not blocks:
        return (
            "No annotations found. Only annotations made in Zotero's built-in reader are synced; "
            "annotations stored inside the PDF file by an external reader must first be imported in Zotero."
        )

    return f"Found {total} annotation(s):\n\n" + "\n\n===\n\n".join(blocks)


@mcp.tool()
def create_note(item_key: str, note_html: str, tags: list[str] | None = None) -> str:
    """Create a new note attached to a paper. note_html should be HTML (e.g. '<p>text</p>'). Optionally pass tags."""
    zot = get_zotero()

    payload = {
        "itemType": "note",
        "note": note_html,
        "parentItem": item_key,
        "tags": [{"tag": t} for t in (tags or [])],
    }

    result = zot.create_items([payload])

    successful = result.get("successful", {})
    failed = result.get("failed", {})

    if failed:
        return f"Failed to create note: {failed}"

    if successful:
        created_key = next(iter(successful.values()))["key"]
        return f"Note created with key: {created_key}"

    return f"Unexpected response: {result}"


class Passage(BaseModel):
    text: str = Field(description="The passage to highlight, worded as in the PDF. Small differences "
                                  "(ligatures, line-end hyphens, quote styles, a typo) are tolerated.")
    page: str | None = Field(None, description="Optional page label (printed page number) where the "
                                                "passage starts; speeds up long PDFs.")
    comment: str | None = Field(None, description="Optional comment attached to the highlight.")
    color: str | None = Field(None, description="yellow (default), red, green, blue, purple, magenta, "
                                                 "orange, gray, or #rrggbb.")


def _pdf_attachment(zot, key: str) -> dict:
    data = zot.item(key)["data"]
    if data.get("itemType") == "attachment":
        if data.get("contentType") != "application/pdf":
            raise ValueError(f"Attachment {key} is not a PDF.")
        return data
    pdfs = [c["data"] for c in zot.children(key)
            if c["data"].get("itemType") == "attachment" and c["data"].get("contentType") == "application/pdf"]
    if len(pdfs) == 1:
        return pdfs[0]
    if not pdfs:
        raise ValueError(f"Item {key} has no PDF attachment.")
    listing = "; ".join(f"{p['key']} ({p.get('title') or p.get('filename')})" for p in pdfs)
    raise ValueError(f"Item {key} has several PDFs; pass one of these attachment keys: {listing}")


def _already_covered(part: dict, existing_key: str, pos: dict) -> bool:
    """True if an existing highlight on the same page already covers this passage: same or
    containing text, or (almost) the same start and end position."""
    if pos.get("pageIndex") != part["pageIndex"]:
        return False
    key = ph.keyify(part["text"])
    if key and existing_key and key in existing_key:
        return True
    rects = pos.get("rects")
    if not rects:
        return False
    ends = lambda rs: rs[0] + rs[-1]
    return max(abs(a - b) for a, b in zip(ends(part["rects"]), ends(rects))) < 2


@mcp.tool()
def create_highlights(item_key: str, passages: list[Passage], tags: list[str] | None = None,
                      dry_run: bool = False) -> str:
    """Highlight passages in a paper's PDF as native Zotero highlight annotations (as if made in
    Zotero's reader). item_key is the paper or its PDF attachment (if the paper has several PDFs,
    pass the attachment key). Each passage is found by fuzzy text matching and highlighted exactly as
    given, snapped to whole words; nothing is extended to full sentences, so pass the precise
    passage. A passage crossing a page break becomes one highlight per page. Passages that are already
    highlighted are skipped, so re-running is safe. Use dry_run=true to preview without writing."""
    zot = get_zotero()
    try:
        att = _pdf_attachment(zot, item_key)
    except ValueError as e:
        return str(e)
    if att.get("linkMode") == "linked_file":
        return "This PDF is a linked file (not stored in Zotero), so it can't be downloaded via the API."
    try:
        pdf = zot.file(att["key"])
    except Exception as e:
        return f"Could not download the PDF from Zotero storage ({e}). Is file syncing enabled?"
    if not isinstance(pdf, (bytes, bytearray)) or not bytes(pdf[:1024]).lstrip().startswith(b"%PDF"):
        return "Zotero did not return a PDF file for this attachment."

    existing = []
    for c in zot.everything(zot.children(att["key"])):
        d = c["data"]
        if d.get("itemType") == "annotation" and d.get("annotationType") == "highlight":
            try:
                pos = json.loads(d.get("annotationPosition") or "{}")
            except ValueError:
                pos = {}
            existing.append((ph.keyify(d.get("annotationText", "")), pos))

    results = ph.find_passages(bytes(pdf), [p.model_dump() for p in passages])
    lines, todo = [], []   # todo: (passage number, payload)
    for n, (p, r) in enumerate(zip(passages, results), 1):
        if not r["found"]:
            lines.append(f"{n}. NOT FOUND: “{p.text[:100]}”")
            continue
        try:
            color = ph.color_hex(p.color)
        except ValueError as e:
            lines.append(f"{n}. {e}")
            continue
        pages = "–".join(dict.fromkeys(part["pageLabel"] for part in r["parts"]))
        new = [part for part in r["parts"]
               if not any(_already_covered(part, k, pos) for k, pos in existing)]
        if not new:
            lines.append(f"{n}. p. {pages}: already highlighted, skipped")
            continue
        for part in new:
            todo.append((n, ph.annotation_payload(att["key"], part, color, p.comment or "", tags or [])))
            existing.append((ph.keyify(part["text"]), {"pageIndex": part["pageIndex"], "rects": part["rects"]}))
        fuzzy = "" if r["cost"] == 0 else f" (approximate match, {r['cost']:.0%} difference)"
        shown = r["matched"] if dry_run else (r["matched"][:90] + ("…" if len(r["matched"]) > 90 else ""))
        lines.append(f"{n}. p. {pages}{fuzzy}: “{shown}”")

    title = att.get("title") or att.get("filename") or att["key"]
    if dry_run:
        return (f"DRY RUN for {title} (attachment {att['key']}): {len(todo)} highlight(s) would be created.\n\n"
                + "\n".join(lines))

    created, failed = {}, []
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        res = zot.create_items([payload for _, payload in chunk])
        for idx, v in res.get("successful", {}).items():
            created.setdefault(chunk[int(idx)][0], []).append(v["key"])
        for idx, v in res.get("failed", {}).items():
            failed.append(f"passage {chunk[int(idx)][0]}: {v.get('message', v)}")
    for j, line in enumerate(lines):
        n = int(line.split(".", 1)[0])
        if n in created:
            lines[j] = f"{line}  [key {', '.join(created[n])}]"
    summary = f"Created {sum(map(len, created.values()))} highlight(s) on {title} (attachment {att['key']})."
    if failed:
        summary += "\nFailed: " + "; ".join(failed)
    return summary + "\n\n" + "\n".join(lines)


@mcp.tool()
def delete_annotations(annotation_keys: list[str]) -> str:
    """Delete Zotero annotations (highlights, comments, …) by key, e.g. to undo create_highlights.
    Only items of type annotation are deleted; any other key is refused."""
    zot = get_zotero()
    keys = list(dict.fromkeys(k.strip().upper() for k in annotation_keys if k.strip()))
    found = []
    for i in range(0, len(keys), 50):
        found += zot.items(itemKey=",".join(keys[i:i + 50]))
    anns = [it for it in found if it["data"].get("itemType") == "annotation"]
    ann_keys = {it["key"] for it in anns}
    refused = [k for k in keys if k not in ann_keys]
    deleted = 0
    for i in range(0, len(anns), 50):
        chunk = anns[i:i + 50]
        try:
            zot.delete_item(chunk, last_modified=zot.last_modified_version())
        except Exception as e:  # pyzotero raises on HTTP errors
            return f"Deleted {deleted} annotation(s); then deletion failed: {e}"
        deleted += len(chunk)
    msg = f"Deleted {deleted} annotation(s)."
    if refused:
        msg += f" Not deleted (not found or not annotations): {', '.join(refused)}"
    return msg


MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


def _extract_token(headers, query_string: bytes) -> str:
    """Pull a bearer token from the Authorization header, falling back to ?token=."""
    for k, v in headers:
        if k.lower() == b"authorization":
            val = v.decode()
            if val.lower().startswith("bearer "):
                return val[7:].strip()
    from urllib.parse import parse_qs
    return parse_qs(query_string.decode()).get("token", [""])[0]


def is_authorized(request) -> bool:
    if not MCP_AUTH_TOKEN:
        return True
    token = _extract_token(request.headers.raw, request.url.query.encode())
    return token == MCP_AUTH_TOKEN


# --- SSE transport (Claude web connector) ---
sse_transport = SseServerTransport("/messages/")


async def handle_sse(request):
    from starlette.responses import Response
    if not is_authorized(request):
        return Response("Unauthorized", status_code=401)
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp._mcp_server.run(
            streams[0], streams[1],
            mcp._mcp_server.create_initialization_options(),
        )


# --- Streamable HTTP transport (Mistral Le Chat connector, modern MCP clients) ---
# streamable_http_app() builds the /mcp route AND a lifespan that runs the
# session manager; we extend it with the legacy SSE routes so both work.
app = mcp.streamable_http_app()
app.router.routes.append(Route("/sse", endpoint=handle_sse))
app.router.routes.append(Mount("/messages/", app=sse_transport.handle_post_message))


class TokenAuthMiddleware:
    """Pure-ASGI guard for /mcp (BaseHTTPMiddleware would buffer the SSE stream)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and MCP_AUTH_TOKEN and scope.get("path", "").rstrip("/") == "/mcp":
            token = _extract_token(scope.get("headers", []), scope.get("query_string", b""))
            if token != MCP_AUTH_TOKEN:
                from starlette.responses import PlainTextResponse
                await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = TokenAuthMiddleware(app)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
