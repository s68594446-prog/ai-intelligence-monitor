from typing import Literal

from app.core.settings import Settings
from app.web_discovery.providers import (
    ExaWebSearchAdapter,
    GeminiGoogleSearchAdapter,
    SearXNGWebSearchAdapter,
    TheNewsAPIAdapter,
    TavilyWebSearchAdapter,
    YouWebSearchAdapter,
)
from app.web_discovery.search import BraveWebSearchAdapter, WebSearchError


ProviderName = Literal["brave", "searxng", "tavily", "exa", "you", "gemini_google"]
PROVIDER_NAMES = ("brave", "searxng", "tavily", "exa", "you", "gemini_google")
NEWS_PROVIDER_NAMES = ("thenewsapi",)


def build_provider(name: str, settings: Settings, *, client=None, sleep_fn=None):
    if name not in PROVIDER_NAMES:
        raise WebSearchError("UNKNOWN_PROVIDER")
    common = {
        "client": client,
        "timeout_seconds": settings.web_search_timeout_seconds,
        "max_retries": settings.web_search_max_retries,
    }
    if sleep_fn is not None:
        common["sleep_fn"] = sleep_fn
    if name == "brave":
        return BraveWebSearchAdapter(
            api_key=settings.brave_search_api_key, base_url=settings.brave_search_base_url, **common
        )
    if name == "searxng":
        return SearXNGWebSearchAdapter(image_digest=settings.searxng_image_digest, **common)
    if name == "tavily":
        return TavilyWebSearchAdapter(api_key=settings.tavily_api_key, **common)
    if name == "exa":
        return ExaWebSearchAdapter(api_key=settings.exa_api_key, **common)
    if name == "you":
        return YouWebSearchAdapter(api_key=settings.you_api_key, **common)
    return GeminiGoogleSearchAdapter(api_key=settings.gemini_api_key, model=settings.gemini_search_model, **common)


class RoutedWebSearchAdapter:
    name = "provider_router"
    version = "web-search-router-v1"

    def __init__(self, primary, fallbacks=(), *, max_fallbacks=1):
        self.primary = primary
        seen = {primary.name}
        self.fallbacks = []
        for adapter in fallbacks:
            if adapter.name not in seen:
                self.fallbacks.append(adapter)
                seen.add(adapter.name)
        self.max_fallbacks = min(max_fallbacks, 1)
        self.audit: list[dict] = []

    def search(self, query, *, count):
        try:
            page = self.primary.search(query, count=count)
            self.audit.append({
                "query_id": query.query_id, "provider": self.primary.name,
                "outcome": "SUCCESS", "fallback": False,
                "request_count": page.request_count, "retry_count": page.retry_count,
            })
            return page
        except WebSearchError as exc:
            self.audit.append({
                "query_id": query.query_id, "provider": self.primary.name,
                "outcome": exc.code, "fallback": False,
                "request_count": exc.request_count, "retry_count": exc.retry_count,
            })
            if not exc.retryable or self.max_fallbacks == 0 or not self.fallbacks:
                raise
            fallback = self.fallbacks[0]
            try:
                page = fallback.search(query, count=count)
            except WebSearchError as fallback_exc:
                self.audit.append({
                    "query_id": query.query_id, "provider": fallback.name,
                    "outcome": fallback_exc.code, "fallback": True,
                    "request_count": fallback_exc.request_count,
                    "retry_count": fallback_exc.retry_count,
                })
                raise WebSearchError(
                    f"{self.primary.name.upper()}_{exc.code}_FALLBACK_{fallback.name.upper()}_{fallback_exc.code}",
                    retryable=fallback_exc.retryable,
                    request_count=exc.request_count + fallback_exc.request_count,
                    retry_count=exc.retry_count + fallback_exc.retry_count,
                ) from fallback_exc
            self.audit.append({
                "query_id": query.query_id, "provider": fallback.name,
                "outcome": "SUCCESS", "fallback": True,
                "request_count": page.request_count, "retry_count": page.retry_count,
            })
            return page.model_copy(update={"fallback_from": self.primary.name})

    def close(self):
        self.primary.close()
        for adapter in self.fallbacks:
            adapter.close()


def build_routed_provider(settings: Settings, *, selected: str | None = None, clients=None, sleep_fn=None):
    primary_name = selected or settings.web_search_provider
    clients = clients or {}
    primary = build_provider(primary_name, settings, client=clients.get(primary_name), sleep_fn=sleep_fn)
    fallback_names = [name.strip() for name in settings.web_search_fallbacks.split(",") if name.strip()]
    fallbacks = []
    for name in fallback_names:
        if name == primary_name:
            continue
        try:
            fallbacks.append(build_provider(name, settings, client=clients.get(name), sleep_fn=sleep_fn))
        except WebSearchError as exc:
            if exc.code != "MISSING_API_KEY":
                raise
            continue
        if len(fallbacks) >= settings.web_search_max_fallbacks:
            break
    return RoutedWebSearchAdapter(primary, fallbacks, max_fallbacks=settings.web_search_max_fallbacks)


def build_news_provider(name: str, settings: Settings, *, client=None, sleep_fn=None, clock=None):
    if name not in NEWS_PROVIDER_NAMES:
        raise WebSearchError("UNKNOWN_NEWS_PROVIDER")
    common = {
        "client": client,
        "timeout_seconds": settings.web_search_timeout_seconds,
        "max_retries": settings.web_search_max_retries,
        "clock": clock,
    }
    if sleep_fn is not None:
        common["sleep_fn"] = sleep_fn
    return TheNewsAPIAdapter(api_key=settings.thenewsapi_key, **common)
