#!/usr/bin/env python3
"""
RSS to Discord webhook notifier
Reads config from a YAML or JSON file, saves seen entry state to a JSON file.
"""

import html.parser
import json
import re
import sys
import urllib.request
import urllib.error
import pathlib
import time
import logging
import argparse

import feedparser

DESCRIPTION_MAX = 300


class _TagStripper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


class _OGImageParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.og_image: str | None = None
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == "body":
            self._done = True
        elif tag == "meta":
            d = dict(attrs)
            if d.get("property") == "og:image" and d.get("content"):
                self.og_image = d["content"]
                self._done = True


def strip_html(text: str) -> str:
    p = _TagStripper()
    try:
        p.feed(text)
    except Exception:
        pass
    return p.get_text()


# Escapes block-level markers (# > at line start) and common inline markers
_MD_ESCAPE_RE = re.compile(r'(?m)(^[>#]+|[*_`~])')


def escape_markdown(text: str) -> str:
    return _MD_ESCAPE_RE.sub(r'\\\1', text)


def fetch_og_image(url: str) -> str | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rss-discord/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            chunk = resp.read(32768).decode("utf-8", errors="replace")
        p = _OGImageParser()
        p.feed(chunk)
        return p.og_image
    except Exception as exc:
        log.debug("Could not fetch OG image from %s: %s", url, exc)
        return None

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def load_config(path: pathlib.Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def load_state(path: pathlib.Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def post_to_discord(
    webhook_url: str,
    entry,
    feed_title: str,
    description: str = "",
    image_url: str | None = None,
    debug: bool = False,
) -> None:
    title = entry.get("title", "(no title)").strip()
    link = entry.get("link", "").strip()
    source = feed_title.strip() if feed_title else None

    embed = {
        "title": title[:256],
        "url": link or None,
        "color": 5793266,  # neutral blue
    }
    if description:
        embed["description"] = description
    if image_url:
        embed["image"] = {"url": image_url}
    if source:
        embed["footer"] = {"text": source}

    payload_dict = {"embeds": [embed]}
    payload = json.dumps(payload_dict).encode()

    if debug:
        log.debug("Webhook URL: %s", webhook_url)
        log.debug("Payload: %s", json.dumps(payload_dict, indent=2))

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "rss-discord/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                log.warning("Unexpected Discord response: %s", resp.status)
            elif debug:
                log.debug("Discord response status: %s", resp.status)
    except urllib.error.HTTPError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.code, e.reason)
        raise
    except urllib.error.URLError as e:
        log.error("Discord webhook connection error: %s", e.reason)
        raise


def process_feed(feed_cfg: dict, seen: set, debug: bool = False) -> tuple[list[str], bool]:
    """Parse feed, post new entries, return (current entry IDs, posted_any).

    In debug mode posts at most one entry and does not update state.
    """
    url = feed_cfg["url"]
    webhook = feed_cfg["webhook"]

    log.debug("Fetching feed: %s", url)
    parsed = feedparser.parse(url)

    if parsed.bozo and not parsed.entries:
        log.warning("Failed to parse feed %s: %s", url, parsed.bozo_exception)
        return list(seen), False

    feed_title = feed_cfg.get("name") or getattr(parsed.feed, "title", url)
    log.debug("[%s] Total entries in feed: %d", feed_title, len(parsed.entries))

    current_ids = []
    new_entries = []

    for entry in parsed.entries:
        eid = entry.get("id") or entry.get("link") or entry.get("title")
        if not eid:
            continue
        current_ids.append(eid)
        if eid not in seen:
            new_entries.append((eid, entry))
            log.debug("[%s] New entry: %s", feed_title, eid[:120])
        else:
            log.debug("[%s] Already seen: %s", feed_title, eid[:120])

    log.debug("[%s] New entries to post: %d", feed_title, len(new_entries))

    posted_any = False
    for eid, entry in reversed(new_entries):
        try:
            raw_desc = entry.get("summary") or entry.get("description", "")
            description = strip_html(raw_desc).strip()
            if len(description) > DESCRIPTION_MAX:
                description = description[:DESCRIPTION_MAX].rstrip() + "…"
            description = escape_markdown(description)

            link = entry.get("link", "")
            log.debug("[%s] Fetching OG image for %s", feed_title, link[:80])
            image_url = fetch_og_image(link)
            log.debug("[%s] OG image: %s", feed_title, image_url)

            post_to_discord(webhook, entry, feed_title, description=description, image_url=image_url, debug=debug)
            log.info("[%s] Posted: %s", feed_title, entry.get("title", eid)[:80])
            posted_any = True
            if debug:
                return current_ids, True
            time.sleep(1)
        except Exception:
            log.error("Skipping entry %s due to post failure", eid)

    return current_ids, posted_any


def main():
    parser = argparse.ArgumentParser(description="RSS to Discord webhook notifier")
    parser.add_argument("config", help="Path to config file (YAML or JSON)")
    parser.add_argument(
        "--state",
        default=None,
        help="Path to state file (default: <config_dir>/state.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Post one entry (dry-run for state), verbose output",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("Debug mode enabled — will post at most one entry and skip state save")

    config_path = pathlib.Path(args.config).expanduser().resolve()
    state_path = (
        pathlib.Path(args.state).expanduser().resolve()
        if args.state
        else config_path.parent / "state.json"
    )

    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    log.debug("Config: %s", config_path)
    log.debug("State:  %s", state_path)

    config = load_config(config_path)
    state = load_state(state_path)

    feeds = config.get("feeds", [])
    if not feeds:
        log.error("No feeds defined in config.")
        sys.exit(1)

    for feed_cfg in feeds:
        url = feed_cfg.get("url")
        if not url or not feed_cfg.get("webhook"):
            log.warning("Skipping incomplete feed entry: %s", feed_cfg)
            continue
        seen = set(state.get(url, []))
        current_ids, posted = process_feed(feed_cfg, seen, debug=args.debug)
        if not args.debug:
            # Only keep IDs still present in the feed to avoid unbounded growth
            state[url] = current_ids
        if args.debug and posted:
            log.debug("Debug mode: stopping after first posted entry")
            break

    if args.debug:
        log.debug("Debug mode: state not saved")
    else:
        save_state(state_path, state)
        log.info("Done. State saved to %s", state_path)


if __name__ == "__main__":
    main()
