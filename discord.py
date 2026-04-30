import json
import urllib.request
import urllib.error
import logging

from feed import FeedEntry

log = logging.getLogger(__name__)


def post_to_discord(
    webhook_url: str,
    entry: FeedEntry,
    debug: bool = False,
) -> None:
    embed = {
        "title": entry.title,
        "url": entry.link or None,
        "color": 5793266,
    }
    if entry.description:
        embed["description"] = entry.description
    if entry.image_url:
        embed["image"] = {"url": entry.image_url}
    if entry.feed_title:
        embed["footer"] = {"text": entry.feed_title}

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
