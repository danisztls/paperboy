from abc import ABC, abstractmethod

from playwright.async_api import Page

from pipeline import Item


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
