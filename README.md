# RSS Watcher for Notion DB

A small Python RSS watcher that reads an RSS feed and upserts feed items into a Notion database with a consistent shape.

The default feed is the IACR ePrint RSS feed:

```text
https://eprint.iacr.org/rss/rss.xml?format=nonstandard
```

## Configuration

Set these environment variables:

| Variable | Required | Description |
| --- | --- | --- |
| `NOTION_TOKEN` | yes | Internal integration token for the Notion API. |
| `NOTION_DATABASE_ID` | no | Target Notion database ID. If omitted, the watcher searches for a database named by `NOTION_DATABASE_NAME`. |
| `NOTION_DATABASE_NAME` | no | Database title to find through Notion search. Defaults to `RSS Feeds`. |
| `RSS_URL` | no | RSS URL to ingest. Defaults to the IACR ePrint feed above. |
| `RSS_SOURCE_NAME` | no | Source label written to Notion. Defaults to `IACR ePrint`. |
| `RSS_LIMIT` | no | Maximum items to process from the feed. Defaults to all items. |
| `RSS_USER_AGENT` | no | User-Agent used for RSS fetches. Defaults to a browser-like Chrome User-Agent to avoid feeds rejecting generic crawler clients. |
| `RSS_SEEN_STATE_PATH` | no | Local JSON file used to remember RSS items that have already been parsed. Defaults to `rss_seen_state.json`. |
| `RSS_BOOTSTRAP_SEEN` | no | When `1`, the first run records all currently visible RSS items as already seen and creates nothing. Defaults to `1`. |
| `NOTION_VERSION` | no | Notion API version. Defaults to `2022-06-28`. |

## Expected Notion properties

The watcher can find a Notion database named `RSS Feeds` when `NOTION_DATABASE_ID` is not provided. It inspects the target database schema and writes only matching properties. It supports common RSS database fields such as:

- `Name` / `Title` as the title property
- `URL` / `Link` as the item URL
- `Source`
- `Published` / `Date`
- `Summary` / `Description`
- `GUID`
- `Authors` / `Author`
- `Tags` / `Categories`
- `Status` / `Read`

Freshness is tracked by the local seen-state file. Each normal run first filters out RSS items whose URL/GUID is already in `rss_seen_state.json`; for each remaining fresh item, it runs a filtered Notion query with `page_size=1` to check whether that URL/GUID already exists before creating a page.
If you manually delete a Notion item, the watcher will not re-add it because the RSS item remains recorded in the local seen-state file.

On the first run, the watcher bootstraps `rss_seen_state.json` with all currently visible RSS items and creates nothing. This prevents old feed items, including ones you deleted from Notion, from being imported as if they were new. After that, only RSS items that appear for the first time are eligible to be created.
Set `RSS_BOOTSTRAP_SEEN=0` only if you intentionally want the first run to import missing current feed items.

List-valued feed fields, such as authors and categories, are written as clean text or native Notion select values instead of Python list formatting like `['value']`.
The RSS item abstract/description is also written into the Notion page body as a paragraph, truncated to 2000 characters.
Inline math surrounded with `$...$` in the description is sent to Notion as equation rich text.

## Run

```bash
python3 rss_watcher.py
```

For a one-off run against the default IACR feed and a database named `RSS Feeds`:

```bash
NOTION_TOKEN=secret_... python3 rss_watcher.py
```

If IACR rejects an automated runtime, override the RSS headers instead of relying on a generic crawler identity:

```bash
RSS_USER_AGENT="Mozilla/5.0 ..." NOTION_TOKEN=secret_... python3 rss_watcher.py
```

## Background watcher on Windows

The CLI daemon can run the watcher in the background every hour and install a Windows Task Scheduler entry.
Use the same Python 3 executable for setup and startup; the scheduled task reuses the interpreter that runs `rss_daemon.py`.

Create an ignored local `.env` file:

```powershell
python rss_daemon.py --init-env
notepad .env
```

Fill in at least these values:

```text
NOTION_TOKEN=secret_...
NOTION_DATABASE_NAME=RSS Feeds
RSS_URL=https://eprint.iacr.org/rss/rss.xml?format=nonstandard
RSS_SOURCE_NAME=IACR ePrint
RSS_INTERVAL_MINUTES=60
RSS_SEEN_STATE_PATH=
RSS_BOOTSTRAP_SEEN=1
```

Then run:

```powershell
python rss_daemon.py --once --limit 1
python rss_daemon.py --start
python rss_daemon.py --install-startup
python rss_daemon.py --status
```

Useful management commands:

```powershell
python rss_daemon.py --stop
python rss_daemon.py --uninstall-startup
```

The daemon reads settings from `rss_config.json`, then `.env`, then real environment variables. Later sources override earlier ones.
Token-bearing local files such as `.env`, `.env.*`, and `rss_config.json` are ignored by git.
The seen-state file `rss_seen_state.json` is also ignored by git and should stay on the machine running the daemon.
Logs are written to `rss_watcher.log`, which is also ignored.

`--install-startup` starts the watcher when the Windows user signs in after power on. Starting before sign-in requires an elevated boot task; run `python rss_daemon.py --install-boot` from an administrator PowerShell if you need that behavior.
