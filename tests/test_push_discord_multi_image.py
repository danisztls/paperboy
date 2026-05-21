"""Tests for the multi-image embed-merge behavior of `post_to_discord`."""

import json

import aiohttp
from yarl import URL

from pipeline import Item
from pull.scrapers.vivareal import VivaRealAdapter
from push.discord import post_to_discord
from tests.conftest import WEBHOOK_URL


def _posted_payload(mock_http) -> dict:
    calls = mock_http.requests.get(("POST", URL(WEBHOOK_URL)), [])
    assert calls, "No POST was made to the webhook"
    return json.loads(calls[0].kwargs["data"])


def _item(**kw) -> Item:
    base = dict(id="i1", title="A house", source="VivaReal", url="https://example.com/listing/1")
    base.update(kw)
    return Item(**base)


async def test_multiple_images_merge_into_shared_url_embeds(mock_http):
    mock_http.post(WEBHOOK_URL, status=204)
    item = _item(images=["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"])

    async with aiohttp.ClientSession() as session:
        await post_to_discord(WEBHOOK_URL, item, session)

    payload = _posted_payload(mock_http)
    embeds = payload["embeds"]
    assert len(embeds) == 3
    assert all(e["url"] == item.url for e in embeds)
    assert [e["image"]["url"] for e in embeds] == item.images
    # Title/description/footer live on the first embed only.
    assert embeds[0]["title"] == "A house"
    assert "title" not in embeds[1]
    assert "title" not in embeds[2]


async def test_images_capped_at_four(mock_http):
    mock_http.post(WEBHOOK_URL, status=204)
    urls = [f"https://img/{i}.jpg" for i in range(6)]
    item = _item(images=urls)

    async with aiohttp.ClientSession() as session:
        await post_to_discord(WEBHOOK_URL, item, session)

    payload = _posted_payload(mock_http)
    assert len(payload["embeds"]) == 4
    assert [e["image"]["url"] for e in payload["embeds"]] == urls[:4]


async def test_legacy_single_image_path(mock_http):
    mock_http.post(WEBHOOK_URL, status=204)
    item = _item(image="https://img/only.jpg")

    async with aiohttp.ClientSession() as session:
        await post_to_discord(WEBHOOK_URL, item, session)

    payload = _posted_payload(mock_http)
    assert len(payload["embeds"]) == 1
    assert payload["embeds"][0]["image"] == {"url": "https://img/only.jpg"}


async def test_skip_image_suppresses_all_images(mock_http):
    mock_http.post(WEBHOOK_URL, status=204)
    item = _item(images=["https://img/1.jpg", "https://img/2.jpg"])

    async with aiohttp.ClientSession() as session:
        await post_to_discord(WEBHOOK_URL, item, session, skip_image=True)

    payload = _posted_payload(mock_http)
    assert len(payload["embeds"]) == 1
    assert "image" not in payload["embeds"][0]


async def test_no_url_degrades_to_single_image(mock_http):
    """Without entry.url, the embed-merge trick won't render — degrade to one embed."""
    mock_http.post(WEBHOOK_URL, status=204)
    item = _item(url=None, images=["https://img/1.jpg", "https://img/2.jpg"])

    async with aiohttp.ClientSession() as session:
        await post_to_discord(WEBHOOK_URL, item, session)

    payload = _posted_payload(mock_http)
    assert len(payload["embeds"]) == 1
    assert payload["embeds"][0]["image"] == {"url": "https://img/1.jpg"}


async def test_images_dedup(mock_http):
    mock_http.post(WEBHOOK_URL, status=204)
    item = _item(images=["https://img/1.jpg", "https://img/1.jpg", "https://img/2.jpg"])

    async with aiohttp.ClientSession() as session:
        await post_to_discord(WEBHOOK_URL, item, session)

    payload = _posted_payload(mock_http)
    assert [e["image"]["url"] for e in payload["embeds"]] == [
        "https://img/1.jpg",
        "https://img/2.jpg",
    ]


# ----- VivaReal adapter unit tests (multi-image extraction) -----


def test_vivareal_jsonld_extracts_up_to_four_images():
    adapter = VivaRealAdapter()
    item = adapter._parse_jsonld_entry(
        {
            "@type": "Apartment",
            "url": "https://www.vivareal.com.br/imovel/foo/",
            "name": "Apartamento em Centro, Cidade",
            "numberOfBedrooms": 2,
            "numberOfBathroomsTotal": 1,
            "floorSize": {"value": 65},
            "address": {"addressLocality": "Cidade", "streetAddress": "Rua X"},
            "offers": {"price": 1500},
            "image": [f"https://img/v/{i}.jpg" for i in range(6)],
        }
    )
    assert item is not None
    assert item.images == [f"https://img/v/{i}.jpg" for i in range(4)]
    assert item.image == "https://img/v/0.jpg"


def test_vivareal_jsonld_string_image_becomes_singleton():
    adapter = VivaRealAdapter()
    item = adapter._parse_jsonld_entry(
        {
            "@type": "Apartment",
            "url": "https://www.vivareal.com.br/imovel/bar/",
            "name": "Apartamento em Centro, Cidade",
            "address": {"addressLocality": "Cidade"},
            "image": "https://img/v/solo.jpg",
        }
    )
    assert item is not None
    assert item.images == ["https://img/v/solo.jpg"]
    assert item.image == "https://img/v/solo.jpg"
