# src/autoi18n/page_registry.py
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class RegisteredPage:
    page_name: str
    html_getter: Callable[..., str]
    target_langs: List[str]
    context: Optional[Dict[str, Any]] = field(default_factory=dict)


class PageRegistry:
    def __init__(self) -> None:
        self._pages: Dict[str, RegisteredPage] = {}

    def register_page(
        self,
        page_name: str,
        html_getter: Callable[..., str],
        target_langs: List[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not page_name:
            raise ValueError("page_name is required")
        if not callable(html_getter):
            raise ValueError("html_getter must be callable")
        if not target_langs:
            raise ValueError("target_langs must not be empty")

        self._pages[page_name] = RegisteredPage(
            page_name=page_name,
            html_getter=html_getter,
            target_langs=list(target_langs),
            context=context or {},
        )

    def get_page(self, page_name: str) -> RegisteredPage:
        try:
            return self._pages[page_name]
        except KeyError as exc:
            raise KeyError(f"Page '{page_name}' is not registered") from exc

    def list_pages(self) -> List[RegisteredPage]:
        return list(self._pages.values())

    def clear(self) -> None:
        self._pages.clear()