import io
import json
import logging

import aiohttp

from feed import FeedEntry

log = logging.getLogger(__name__)

_MAX_BYTES = 4 * 1024 * 1024
_MAX_DIM = 2000


async def _fetch_image(url: str, session: aiohttp.ClientSession) -> bytes | None:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if not ct.startswith("image/"):
                return None
            return await resp.read()
    except Exception as exc:
        log.debug("Could not download image %s: %s", url, exc)
        return None


def _optimize_image(data: bytes) -> bytes | None:
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        needs_resize = max(img.size) > _MAX_DIM
        if len(data) <= _MAX_BYTES and not needs_resize:
            return data
        if needs_resize:
            img.thumbnail((_MAX_DIM, _MAX_DIM), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=85)
        result = buf.getvalue()
        return result if len(result) <= 8 * 1024 * 1024 else None
    except Exception as exc:
        log.debug("Could not optimize image: %s", exc)
        return None


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
    if entry.feed_title:
        embed["footer"] = {"text": entry.feed_title}

    image_bytes: bytes | None = None
    if entry.image_url:
        raw = await _fetch_image(entry.image_url, session)
        if raw is not None:
            image_bytes = _optimize_image(raw)

    if image_bytes is not None:
        embed["image"] = {"url": "attachment://og_image.webp"}
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps({"embeds": [embed]}), content_type="application/json")
        form.add_field("files[0]", image_bytes, filename="og_image.webp", content_type="image/webp")
        post_kwargs: dict = {"data": form}
    else:
        if entry.image_url:
            embed["image"] = {"url": entry.image_url}
        post_kwargs = {"data": json.dumps({"embeds": [embed]}).encode(), "headers": {"Content-Type": "application/json"}}

    if debug:
        log.debug("Webhook URL: %s", webhook_url)
        log.debug("Payload embed: %s", json.dumps(embed, indent=2))

    try:
        async with session.post(webhook_url, **post_kwargs) as resp:
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
