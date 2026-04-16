# ZoteroMCP

An MCP (Model Context Protocol) server that gives Claude access to your Zotero library, including full-text search and retrieval of indexed papers.

## Features

- Search your Zotero library by keywords
- Retrieve full text of papers (via Zotero's synced PDF index)
- Browse collections
- Fetch detailed metadata for any item

## Requirements

- A Zotero account with file sync enabled (paid plan recommended for full-text sync)
- A Zotero API key with read access
- Python 3.10+

## Setup

### 1. Get your Zotero credentials

1. Go to [zotero.org/settings/keys](https://www.zotero.org/settings/keys)
2. Create a new API key with **read-only** library access
3. Note your **API key**
4. Find your **user ID** at [zotero.org/settings/security](https://www.zotero.org/settings/security) under the **Applications** section — listed as "Your user ID for use in API calls is XXXX". This value is what goes in `ZOTERO_LIBRARY_ID`

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file or export these variables:

```bash
ZOTERO_API_KEY=your_api_key_here
ZOTERO_LIBRARY_ID=your_numeric_user_id
ZOTERO_LIBRARY_TYPE=user   # or "group" for a group library
```

### 4. Run locally

```bash
python server.py
```

The server listens on port `8080` by default. Set `PORT` to override.

## Deploy to Railway

1. Push this repo to GitHub
2. Create a new project on [railway.com](https://railway.com) from the repo
3. Add the following environment variables in Railway's dashboard:
   - `ZOTERO_API_KEY`
   - `ZOTERO_LIBRARY_ID`
   - `ZOTERO_LIBRARY_TYPE` (optional, defaults to `user`)
   - `MCP_AUTH_TOKEN` — a secret string you choose; required to protect the server from unauthorized access
4. Railway will detect the `Procfile` and deploy automatically

## Connect Claude

### claude.ai (web)

Go to **Settings → Integrations**, add the SSE URL, and set a custom header:

- URL: `https://your-app.railway.app/sse?token=your-secret-token` (token must match `MCP_AUTH_TOKEN` in Railway)

### Claude Desktop

Add the server to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "zotero": {
      "url": "https://zoteromcp-production.up.railway.app/sse"
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `search_papers` | Search library by keywords; returns title, authors, year, venue, abstract |
| `get_paper_fulltext` | Retrieve the full indexed text of a paper by its item key |
| `get_paper_metadata` | Get all metadata fields for a specific item |
| `list_collections` | List all collections with their names and keys |
| `get_collection_papers` | Get papers within a specific collection |

## Notes on Full-Text Access

Full text is fetched from Zotero's web API — it reflects whatever your local Zotero client has indexed and synced. If a paper shows no full text, check that:

- The PDF is attached in Zotero desktop
- Full-text indexing is enabled (Preferences → Search → Full-Text Cache)
- The library has synced recently
