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
class ExistingFeedKeys:
    urls: set[str]
    guids: set[str]


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


def rich_text(value: Any) -> list[dict[str, Any]]:
    text = feed_text(value)
    if not text:
        return []
    return [{"type": "text", "text": {"content": text[:2000]}}]


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
    field_values = [
        (choose_property(properties, ("Name", "Title"), "title"), item.title),
        (choose_property(properties, ("URL", "Link", "Article URL")), item.url),
        (choose_property(properties, ("Source", "Feed")), source_name),
        (choose_property(properties, ("Published", "Publication Date", "Date", "Pub Date")), item.published),
        (choose_property(properties, ("Summary", "Description", "Abstract")), item.summary),
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
            "paragraph": {"rich_text": rich_text(abstract)},
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


def query_existing(database_id: str, token: str, field: str, property_type: str, value: str) -> str | None:
    if property_type not in {"url", "rich_text", "title"} or not value:
        return None
    filter_type = "rich_text" if property_type == "title" else property_type
    payload = {"filter": {"property": field, filter_type: {"equals": value}}, "page_size": 1}
    result = request_json(f"https://api.notion.com/v1/databases/{database_id}/query", token, "POST", payload)
    results = result.get("results", [])
    return results[0]["id"] if results else None


def find_existing_page(database_id: str, token: str, schema: dict[str, Any], item: FeedItem) -> str | None:
    properties = schema["properties"]
    for field, value in (
        (choose_property(properties, ("URL", "Link", "Article URL")), item.url),
        (choose_property(properties, ("GUID", "Guid", "ID", "External ID")), item.guid),
    ):
        if field:
            page_id = query_existing(database_id, token, field, properties[field]["type"], value)
            if page_id:
                return page_id
    return None


def plain_text(parts: list[dict[str, Any]]) -> str:
    return "".join(part.get("plain_text", "") for part in parts).strip()


def page_property_text(page: dict[str, Any], field: str, property_type: str) -> str:
    value = page.get("properties", {}).get(field, {})
    if property_type == "url":
        return (value.get("url") or "").strip()
    if property_type == "rich_text":
        return plain_text(value.get("rich_text", []))
    if property_type == "title":
        return plain_text(value.get("title", []))
    return ""


def collect_existing_feed_keys(database_id: str, token: str, schema: dict[str, Any]) -> ExistingFeedKeys:
    properties = schema["properties"]
    url_field = choose_property(properties, ("URL", "Link", "Article URL"))
    guid_field = choose_property(properties, ("GUID", "Guid", "ID", "External ID"))
    if not url_field and not guid_field:
        raise SystemExit("The target database needs a URL/Link or GUID/ID property to detect existing feed items.")

    existing = ExistingFeedKeys(urls=set(), guids=set())
    cursor = None
    while True:
        payload: dict[str, Any] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        result = request_json(f"https://api.notion.com/v1/databases/{database_id}/query", token, "POST", payload)
        for page in result.get("results", []):
            if url_field:
                value = page_property_text(page, url_field, properties[url_field]["type"])
                if value:
                    existing.urls.add(value)
            if guid_field:
                value = page_property_text(page, guid_field, properties[guid_field]["type"])
                if value:
                    existing.guids.add(value)
        if not result.get("has_more"):
            return existing
        cursor = result.get("next_cursor")


def item_exists(item: FeedItem, existing: ExistingFeedKeys) -> bool:
    return item.url in existing.urls or item.guid in existing.guids


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


def create_new_items(database_id: str, token: str, items: list[FeedItem], source_name: str) -> tuple[int, int]:
    schema = request_json(f"https://api.notion.com/v1/databases/{database_id}", token)
    existing = collect_existing_feed_keys(database_id, token, schema)
    created = 0
    skipped = 0
    for item in items:
        if item_exists(item, existing):
            skipped += 1
            continue
        request_json(
            "https://api.notion.com/v1/pages",
            token,
            "POST",
            build_page_payload(database_id, schema, item, source_name),
        )
        existing.urls.add(item.url)
        existing.guids.add(item.guid)
        created += 1
    return created, skipped


def main() -> int:
    rss_url = os.getenv("RSS_URL", DEFAULT_RSS_URL)
    source_name = os.getenv("RSS_SOURCE_NAME", DEFAULT_SOURCE_NAME)
    token = getenv_required("NOTION_TOKEN")
    database_id = resolve_database_id(token)
    items = parse_rss(fetch_text(rss_url))
    limit = os.getenv("RSS_LIMIT")
    if limit:
        items = items[: int(limit)]
    created, skipped = create_new_items(database_id, token, items, source_name)
    print(f"Processed {len(items)} feed items from {rss_url}; created={created}, skipped_existing={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
