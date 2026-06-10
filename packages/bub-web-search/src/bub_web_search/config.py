from typing import Literal

import bub
from pydantic_settings import SettingsConfigDict

DEFAULT_OLLAMA_API_BASE = "https://ollama.com/api"
DEFAULT_SEARXNG_TIMEOUT_SECONDS = 10
DEFAULT_SEARXNG_SAFE_SEARCH = 1
DEFAULT_SEARXNG_USER_AGENT = "bub-web-search/1.0"


@bub.config(name="web-search")
class WebSearchSettings(bub.Settings):
    model_config = SettingsConfigDict(
        env_prefix="BUB_SEARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    provider: Literal["ollama", "searxng"] | None = None

    ollama_api_key: str | None = None
    ollama_api_base: str = DEFAULT_OLLAMA_API_BASE

    searxng_base_url: str | None = None
    searxng_timeout_seconds: int = DEFAULT_SEARXNG_TIMEOUT_SECONDS
    searxng_default_language: str | None = None
    searxng_default_safe_search: int = DEFAULT_SEARXNG_SAFE_SEARCH
    searxng_user_agent: str = DEFAULT_SEARXNG_USER_AGENT
    searxng_auth_header: str | None = None
    searxng_auth_value: str | None = None

    @property
    def resolved_provider(self) -> Literal["ollama", "searxng"] | None:
        if self.provider is not None:
            return self.provider
        if self.ollama_api_key:
            return "ollama"
        if self.searxng_base_url:
            return "searxng"
        return None

    @property
    def resolved_searxng_base_url(self) -> str | None:
        if self.searxng_base_url is None:
            return None
        base_url = self.searxng_base_url.strip().rstrip("/")
        return base_url or None

    @property
    def resolved_searxng_timeout_seconds(self) -> int:
        return max(1, self.searxng_timeout_seconds)

    @property
    def resolved_searxng_default_safe_search(self) -> int:
        if self.searxng_default_safe_search in {0, 1, 2}:
            return self.searxng_default_safe_search
        return DEFAULT_SEARXNG_SAFE_SEARCH

    @property
    def resolved_searxng_user_agent(self) -> str:
        user_agent = self.searxng_user_agent.strip()
        return user_agent or DEFAULT_SEARXNG_USER_AGENT

    @property
    def resolved_searxng_auth_headers(self) -> dict[str, str]:
        if self.searxng_auth_header is None or self.searxng_auth_value is None:
            return {}
        header_name = self.searxng_auth_header.strip()
        header_value = self.searxng_auth_value.strip()
        if not header_name or not header_value:
            return {}
        return {header_name: header_value}
