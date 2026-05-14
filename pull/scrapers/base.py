from abc import ABC, abstractmethod

from playwright.async_api import Page

from pipeline import Item

_REGISTRY: dict[str, type[SiteAdapter]] = {}


def register_adapter(name: str):
    """Decorator: register a SiteAdapter subclass under `name`."""

    def _wrap(cls: type[SiteAdapter]) -> type[SiteAdapter]:
        _REGISTRY[name] = cls
        return cls

    return _wrap


def get_adapter(name: str) -> type[SiteAdapter] | None:
    return _REGISTRY.get(name)


def available_adapters() -> list[str]:
    return sorted(_REGISTRY)


class SiteAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def scrape(
        self,
        url: str,
        cfg: dict,
        seen: set[str],
        page: Page,
    ) -> list[Item]:
        """Navigate to url and return all currently visible listings.

        Returns ALL items on the page — the caller filters against seen.
        Return [] on failure rather than raising.
        """
        ...
