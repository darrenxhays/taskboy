"""read-only Confluence Cloud search and page retrieval tools."""

import json
import logging
import re
from html.parser import HTMLParser

from taskboy.adapters._util import AccessDenied, _error, _text, wrap
from taskboy.models import Task
from taskboy.redact import redactor
from taskboy.store import Store

logger = logging.getLogger("taskboy.confluence")


class ConfluenceAdapter:
    def __init__(self, store: Store, task: Task, site: str, email: str, api_token: str, spaces: list[str]):
        self.store = store
        self.task = task
        self.site = site.rstrip("/")
        self.email = email
        self.api_token = api_token
        self.spaces = spaces

    async def search_pages(self, args: dict) -> dict:
        cql = str(args.get("cql") or "").strip()
        if self.spaces:
            allowed = ", ".join(f'"{space.replace(chr(34), chr(92) + chr(34))}"' for space in self.spaces)
            cql = f"({cql}) AND space in ({allowed})" if cql else f"space in ({allowed})"
        max_results = min(max(int(args.get("max_results", 10)), 1), 25)
        self.store.add_event(self.task.task_id, "tool_call", {"cql": cql, "max_results": max_results}, tool_name="mcp__confluence__search_pages", is_write=False)
        data = await self._request("GET", "/wiki/rest/api/content/search", params={"cql": cql, "limit": max_results, "expand": "space,version"})
        lines = []
        for page in data.get("results") or []:
            space = (page.get("space") or {}).get("key")
            lines.append(f"{page.get('id')}: {page.get('title')} [{space}] v{(page.get('version') or {}).get('number')}")
        return _text("\n".join(lines) or "no pages matched")

    async def get_page(self, args: dict) -> dict:
        page_id = str(args.get("page_id") or "").strip()
        self.store.add_event(self.task.task_id, "tool_call", {"page_id": page_id}, tool_name="mcp__confluence__get_page", is_write=False)
        data = await self._request("GET", f"/wiki/rest/api/content/{page_id}", params={"expand": "body.storage,space,version"})
        space = str((data.get("space") or {}).get("key") or "")
        if self.spaces and space not in self.spaces:
            return _error(f"page space {space!r} is not on the approved list {self.spaces}")
        body = _html_to_text(str(((data.get("body") or {}).get("storage") or {}).get("value") or ""))
        result = {"id": data.get("id"), "title": data.get("title"), "space": space, "version": (data.get("version") or {}).get("number"), "body": body}
        return _text(json.dumps(result, ensure_ascii=False))

    async def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        import aiohttp

        auth = aiohttp.BasicAuth(self.email, self.api_token)
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.request(method, self.site + path, params=params, headers={"Accept": "application/json"}) as response:
                if response.status in (401, 403):
                    body = redactor.redact(await response.text())[:300]
                    raise AccessDenied("confluence", self.site.split("://", 1)[-1], f"confluence api {method} {path} denied: {response.status} — {body}")
                if response.status >= 300:
                    body = redactor.redact(await response.text())[:300]
                    raise RuntimeError(f"confluence api {method} {path} failed: {response.status} — {body}")
                return await response.json()


class _StorageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self.parts.append("\n")


def _html_to_text(value: str) -> str:
    parser = _StorageText()
    parser.feed(value)
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def build_confluence_server(adapter: ConfluenceAdapter):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = [
        tool("search_pages", "Search readable Confluence pages with CQL. Configured space restrictions are always applied.", {"cql": str, "max_results": int})(wrap(adapter.search_pages, logger)),
        tool("get_page", "Read one Confluence page as plain text. Off-allowlist spaces are refused.", {"page_id": str})(wrap(adapter.get_page, logger)),
    ]
    return create_sdk_mcp_server(name="confluence", version="1.0.0", tools=tools)
