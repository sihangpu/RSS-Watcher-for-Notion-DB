#!/usr/bin/env python3
"""Fetch RSS entries and upsert them into a Notion database."""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_RSS_URL = "https://eprint.iacr.org/rss/rss.xml?format=nonstandard"
DEFAULT_SOURCE_NAME = "IACR ePrint"
DEFAULT_NOTION_VERSION = "2022-06-28"
DEFAULT_DATABASE_NAME = "RSS Feeds"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
RSS_ACCEPT_HEADER = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7"
ABSTRACT_CHARACTER_LIMIT = 2000
NOTION_TEXT_LIMIT = 2000
NOTION_EQUATION_LIMIT = 1000
NOTION_RICH_TEXT_LIMIT = 100
ROOT = Path(__file__).resolve().parent
DEFAULT_SEEN_STATE_PATH = ROOT / "rss_seen_state.json"
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    guid: str
    published: str | None
    summary: str
    authors: list[str]
    categories: list[str]


@dataclass
class SeenFeedState:
    urls: set[str]
    guids: set[str]


@dataclass(frozen=True)
class SyncResult:
    processed: int
    created: int
    skipped_existing: int
    skipped_seen: int
    bootstrapped_seen: int


def getenv_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def request_json(url: str, token: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": os.getenv("NOTION_VERSION", DEFAULT_NOTION_VERSION),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Notion API {method} {url} failed: {exc.code} {body}") from exc


def fetch_text(url: str) -> str:
    headers = {
        "User-Agent": os.getenv("RSS_USER_AGENT", BROWSER_USER_AGENT),
        "Accept": os.getenv("RSS_ACCEPT", RSS_ACCEPT_HEADER),
        "Accept-Language": os.getenv("RSS_ACCEPT_LANGUAGE", "en-US,en;q=0.9"),
        "Cache-Control": "no-cache",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            content_type = response.headers.get("content-type", "")
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"RSS fetch failed with HTTP {exc.code}. The request used a browser-like User-Agent; "
            f"if the origin still blocks the runtime IP, run from an allowed network or configure RSS_USER_AGENT. "
            f"Response body starts with: {body!r}"
        ) from exc
    if "html" in content_type.lower() or not text.lstrip().startswith("<"):
        raise RuntimeError(
            f"RSS endpoint did not return XML (content-type={content_type!r}). "
            "The response may be an HTML bot-block page rather than a feed."
        )
    return text


def child_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for child in element:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names and child.text:
            return child.text.strip()
    return ""


def child_values(element: ET.Element, names: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for child in element:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names and child.text and child.text.strip():
            values.append(child.text.strip())
    return values


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_date(value: str) -> str | None:
    if not value:
        return None
    parsed = email.utils.parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.isoformat()


def parse_rss(xml_text: str) -> list[FeedItem]:
    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    parsed_items: list[FeedItem] = []
    for item in items:
        title = clean_text(child_text(item, ("title",)))
        url = child_text(item, ("link",))
        guid = child_text(item, ("guid", "id")) or url
        summary = clean_text(child_text(item, ("description", "summary")))
        authors = child_values(item, ("creator", "author"))
        categories = child_values(item, ("category",))
        try:
            published = parse_date(child_text(item, ("pubdate", "published", "updated")))
        except (TypeError, ValueError):
            published = None
        if title and url:
            parsed_items.append(FeedItem(title, url, guid, published, summary, authors, categories))
    return parsed_items


def property_by_type(properties: dict[str, Any], property_type: str) -> str | None:
    for name, spec in properties.items():
        if spec.get("type") == property_type:
            return name
    return None


def choose_property(properties: dict[str, Any], candidates: tuple[str, ...], property_type: str | None = None) -> str | None:
    lowered = {name.lower(): name for name in properties}
    for candidate in candidates:
        name = lowered.get(candidate.lower())
        if name and (property_type is None or properties[name].get("type") == property_type):
            return name
    if property_type:
        return property_by_type(properties, property_type)
    return None


def feed_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(part for item in value if (part := feed_text(item)))
    return str(value).strip()


def first_feed_text(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = feed_text(item)
            if text:
                return text
        return ""
    return feed_text(value)


def find_closing_math(value: str, start: int) -> int | None:
    index = start
    while True:
        index = value.find("$", index)
        if index == -1:
            return None
        if index == 0 or value[index - 1] != "\\":
            return index
        index += 1


def append_text(parts: list[dict[str, Any]], value: str) -> None:
    while value and len(parts) < NOTION_RICH_TEXT_LIMIT:
        chunk = value[:NOTION_TEXT_LIMIT]
        parts.append({"type": "text", "text": {"content": chunk}})
        value = value[NOTION_TEXT_LIMIT:]


def append_equation(parts: list[dict[str, Any]], expression: str) -> None:
    expression = expression.strip()[:NOTION_EQUATION_LIMIT]
    if expression and len(parts) < NOTION_RICH_TEXT_LIMIT:
        parts.append({"type": "equation", "equation": {"expression": expression}})


def rich_text(value: Any, *, parse_math: bool = False, limit: int = NOTION_TEXT_LIMIT) -> list[dict[str, Any]]:
    text = feed_text(value)[:limit]
    if not text:
        return []
    if not parse_math:
        parts: list[dict[str, Any]] = []
        append_text(parts, text)
        return parts

    parts = []
    buffer: list[str] = []
    index = 0
    while index < len(text) and len(parts) < NOTION_RICH_TEXT_LIMIT:
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] == "$":
            buffer.append("$")
            index += 2
            continue
        if char != "$":
            buffer.append(char)
            index += 1
            continue

        close_index = find_closing_math(text, index + 1)
        if close_index is None:
            buffer.append(char)
            index += 1
            continue

        expression = text[index + 1 : close_index].strip()
        if not expression:
            buffer.append(text[index : close_index + 1])
            index = close_index + 1
            continue

        if buffer:
            append_text(parts, "".join(buffer))
            buffer = []
        append_equation(parts, expression)
        index = close_index + 1

    if buffer and len(parts) < NOTION_RICH_TEXT_LIMIT:
        append_text(parts, "".join(buffer))
    return parts


def notion_value(property_type: str, value: Any) -> dict[str, Any] | None:
    if value is None or feed_text(value) == "":
        return None
    if property_type == "title":
        return {"title": rich_text(value)}
    if property_type == "rich_text":
        return {"rich_text": rich_text(value)}
    if property_type == "url":
        return {"url": feed_text(value)}
    if property_type == "date":
        return {"date": {"start": feed_text(value)}}
    if property_type == "select":
        name = first_feed_text(value)
        return {"select": {"name": name[:100]}} if name else None
    if property_type == "multi_select":
        values = value if isinstance(value, list) else [value]
        options = [{"name": text[:100]} for item in values if (text := feed_text(item))]
        return {"multi_select": options} if options else None
    if property_type == "checkbox":
        return {"checkbox": bool(value)}
    return None


def build_properties(schema: dict[str, Any], item: FeedItem, source_name: str) -> dict[str, Any]:
    properties = schema["properties"]
    summary_property = choose_property(properties, ("Summary", "Description", "Abstract"))
    field_values = [
        (choose_property(properties, ("Name", "Title"), "title"), item.title),
        (choose_property(properties, ("URL", "Link", "Article URL")), item.url),
        (choose_property(properties, ("Source", "Feed")), source_name),
        (choose_property(properties, ("Published", "Publication Date", "Date", "Pub Date")), item.published),
        (summary_property, item.summary),
        (choose_property(properties, ("GUID", "Guid", "ID", "External ID")), item.guid),
        (choose_property(properties, ("Authors", "Author")), item.authors),
        (choose_property(properties, ("Tags", "Categories", "Category")), item.categories),
        (choose_property(properties, ("Status",)), "Unread"),
        (choose_property(properties, ("Read",)), False),
    ]
    notion_properties: dict[str, Any] = {}
    written: set[str] = set()
    for name, value in field_values:
        if name in written:
            continue
        if not name:
            continue
        if name == summary_property and properties[name]["type"] == "rich_text":
            converted = {"rich_text": rich_text(value, parse_math=True, limit=ABSTRACT_CHARACTER_LIMIT)}
        else:
            converted = notion_value(properties[name]["type"], value)
        if converted is not None:
            notion_properties[name] = converted
            written.add(name)
    return notion_properties


def page_children(item: FeedItem) -> list[dict[str, Any]]:
    abstract = feed_text(item.summary)[:ABSTRACT_CHARACTER_LIMIT].strip()
    if not abstract:
        return []
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text(abstract, parse_math=True, limit=ABSTRACT_CHARACTER_LIMIT)},
        }
    ]


def build_page_payload(database_id: str, schema: dict[str, Any], item: FeedItem, source_name: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "parent": {"database_id": database_id},
        "properties": build_properties(schema, item, source_name),
    }
    children = page_children(item)
    if children:
        payload["children"] = children
    return payload


def page_identity_filter(field: str | None, property_type: str | None, value: str) -> dict[str, Any] | None:
    if property_type not in {"url", "rich_text", "title"} or not field or not value:
        return None
    return {"property": field, property_type: {"equals": value}}


def find_existing_page(database_id: str, token: str, schema: dict[str, Any], item: FeedItem) -> str | None:
    properties = schema["properties"]
    url_field = choose_property(properties, ("URL", "Link", "Article URL"))
    guid_field = choose_property(properties, ("GUID", "Guid", "ID", "External ID"))
    filters = [
        page_identity_filter(url_field, properties[url_field]["type"] if url_field else None, item.url),
        page_identity_filter(guid_field, properties[guid_field]["type"] if guid_field else None, item.guid),
    ]
    filters = [filter_value for filter_value in filters if filter_value]
    if not filters:
        return None
    payload: dict[str, Any] = {
        "filter": filters[0] if len(filters) == 1 else {"or": filters},
        "page_size": 1,
    }
    result = request_json(f"https://api.notion.com/v1/databases/{database_id}/query", token, "POST", payload)
    results = result.get("results", [])
    return results[0]["id"] if results else None


def seen_state_path() -> Path:
    value = os.getenv("RSS_SEEN_STATE_PATH")
    if not value:
        return DEFAULT_SEEN_STATE_PATH
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_seen_state(path: Path | None = None) -> tuple[SeenFeedState, bool]:
    path = seen_state_path() if path is None else path
    if not path.exists():
        return SeenFeedState(urls=set(), guids=set()), False
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object.")
    return (
        SeenFeedState(
            urls=set(data.get("urls", [])),
            guids=set(data.get("guids", [])),
        ),
        True,
    )


def save_seen_state(state: SeenFeedState, path: Path | None = None) -> None:
    path = seen_state_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "urls": sorted(state.urls),
        "guids": sorted(state.guids),
    }
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def bootstrap_seen_enabled() -> bool:
    value = os.getenv("RSS_BOOTSTRAP_SEEN", "1").strip().lower()
    if value in FALSE_VALUES:
        return False
    return value in TRUE_VALUES or value == ""


def item_seen(item: FeedItem, state: SeenFeedState) -> bool:
    return item.url in state.urls or item.guid in state.guids


def mark_seen(item: FeedItem, state: SeenFeedState) -> None:
    if item.url:
        state.urls.add(item.url)
    if item.guid:
        state.guids.add(item.guid)


def mark_items_seen(items: list[FeedItem], state: SeenFeedState) -> None:
    for item in items:
        mark_seen(item, state)


def database_title(database: dict[str, Any]) -> str:
    return "".join(part.get("plain_text", "") for part in database.get("title", []))


def find_database_id(token: str, database_name: str) -> str:
    payload = {
        "query": database_name,
        "filter": {"property": "object", "value": "database"},
        "page_size": 25,
    }
    result = request_json("https://api.notion.com/v1/search", token, "POST", payload)
    databases = result.get("results", [])
    exact_matches = [db for db in databases if database_title(db).casefold() == database_name.casefold()]
    if exact_matches:
        return exact_matches[0]["id"]
    if len(databases) == 1:
        return databases[0]["id"]
    titles = ", ".join(database_title(db) or db.get("id", "<untitled>") for db in databases)
    raise SystemExit(
        f"Could not uniquely find a Notion database named {database_name!r}. "
        f"Set NOTION_DATABASE_ID explicitly. Search results: {titles or 'none'}"
    )


def resolve_database_id(token: str) -> str:
    database_id = os.getenv("NOTION_DATABASE_ID")
    if database_id:
        return database_id
    return find_database_id(token, os.getenv("NOTION_DATABASE_NAME", DEFAULT_DATABASE_NAME))


def create_new_items(
    database_id: str | None,
    token: str,
    items: list[FeedItem],
    source_name: str,
    seen_items: list[FeedItem] | None = None,
    database_name: str | None = None,
) -> SyncResult:
    seen_state, state_exists = load_seen_state()
    if not state_exists and bootstrap_seen_enabled():
        mark_items_seen(seen_items or items, seen_state)
        save_seen_state(seen_state)
        return SyncResult(
            processed=len(items),
            created=0,
            skipped_existing=0,
            skipped_seen=0,
            bootstrapped_seen=len(seen_items or items),
        )

    fresh_items: list[FeedItem] = []
    skipped_seen = 0
    for item in items:
        if item_seen(item, seen_state):
            skipped_seen += 1
        else:
            fresh_items.append(item)

    if not fresh_items:
        return SyncResult(
            processed=len(items),
            created=0,
            skipped_existing=0,
            skipped_seen=skipped_seen,
            bootstrapped_seen=0,
        )

    resolved_database_id = database_id or find_database_id(
        token,
        database_name or os.getenv("NOTION_DATABASE_NAME", DEFAULT_DATABASE_NAME),
    )
    schema = request_json(f"https://api.notion.com/v1/databases/{resolved_database_id}", token)
    created = 0
    skipped_existing = 0
    for item in fresh_items:
        if find_existing_page(resolved_database_id, token, schema, item):
            skipped_existing += 1
            mark_seen(item, seen_state)
            save_seen_state(seen_state)
            continue
        request_json(
            "https://api.notion.com/v1/pages",
            token,
            "POST",
            build_page_payload(resolved_database_id, schema, item, source_name),
        )
        mark_seen(item, seen_state)
        save_seen_state(seen_state)
        created += 1
    return SyncResult(
        processed=len(items),
        created=created,
        skipped_existing=skipped_existing,
        skipped_seen=skipped_seen,
        bootstrapped_seen=0,
    )


def main() -> int:
    rss_url = os.getenv("RSS_URL", DEFAULT_RSS_URL)
    source_name = os.getenv("RSS_SOURCE_NAME", DEFAULT_SOURCE_NAME)
    token = getenv_required("NOTION_TOKEN")
    database_id = os.getenv("NOTION_DATABASE_ID")
    database_name = os.getenv("NOTION_DATABASE_NAME", DEFAULT_DATABASE_NAME)
    all_items = parse_rss(fetch_text(rss_url))
    items = all_items
    limit = os.getenv("RSS_LIMIT")
    if limit:
        items = items[: int(limit)]
    result = create_new_items(database_id, token, items, source_name, seen_items=all_items, database_name=database_name)
    print(
        f"Processed {result.processed} feed items from {rss_url}; "
        f"created={result.created}, skipped_existing={result.skipped_existing}, "
        f"skipped_seen={result.skipped_seen}, bootstrapped_seen={result.bootstrapped_seen}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
