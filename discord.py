import json
import logging

import aiohttp

from feed import FeedEntry

log = logging.getLogger(__name__)


async def post_text_to_discord(
    webhook_url: str,
    text: str,
    session: aiohttp.ClientSession,
    debug: bool = False,
) -> None:
    if len(text) > 2000:
        text = text[:1997] + "…"
    payload = json.dumps({"content": text}).encode()
    if debug:
        log.debug("Webhook URL: %s", webhook_url)
        log.debug("Payload: %s", text[:200])
    try:
        async with session.post(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status not in (200, 204):
                log.warning("Unexpected Discord response: %s", resp.status)
            elif debug:
                log.debug("Discord response status: %s", resp.status)
            if resp.status >= 400:
                body = await resp.text()
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=body,
                )
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise


async def post_to_discord(
    webhook_url: str,
    entry: FeedEntry,
    session: aiohttp.ClientSession,
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

    try:
        async with session.post(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status not in (200, 204):
                log.warning("Unexpected Discord response: %s", resp.status)
            elif debug:
                log.debug("Discord response status: %s", resp.status)
            if resp.status >= 400:
                body = await resp.text()
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=body,
                )
    except aiohttp.ClientResponseError as e:
        log.error("Discord webhook HTTP error: %s - %s", e.status, e.message)
        raise
    except aiohttp.ClientError as e:
        log.error("Discord webhook connection error: %s", e)
        raise
