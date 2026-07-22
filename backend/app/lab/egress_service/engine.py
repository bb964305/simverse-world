"""Real handlers for the three protocol-v2 read-only network tools."""
from __future__ import annotations

import codecs
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import EgressConfig
from .html import bounded_text, extract_html
from .models import EgressUsage
from .pinned_http import EgressFetchError, PinnedHttpClient
from .security import UnsafeEgressTarget, normalize_http_url

_CHARSET = re.compile(r"(?:^|;)\s*charset\s*=\s*['\"]?([^;'\"\s]+)", re.I)
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$|^all$")


class EgressToolError(RuntimeError):
    def __init__(self, code: str, *, usage: EgressUsage | None = None):
        self.code = code
        self.usage = usage or EgressUsage()
        super().__init__(code)


class EgressEngine:
    def __init__(self, config: EgressConfig, *, http: PinnedHttpClient | None = None):
        self.config = config
        self.http = http or PinnedHttpClient(config)

    async def execute(
        self, tool_name: str, args: dict, *, allowlist: list[str]
    ) -> tuple[dict, EgressUsage]:
        if not self.config.enabled:
            raise EgressToolError("egress_disabled")
        if tool_name == "web.search":
            return await self._search(args, allowlist=allowlist)
        if tool_name in {"web.fetch", "browser.navigate"}:
            return await self._fetch(tool_name, args, allowlist=allowlist)
        raise EgressToolError("unsupported_tool")

    async def _search(
        self, args: dict, *, allowlist: list[str]
    ) -> tuple[dict, EgressUsage]:
        if not self.config.search_endpoint:
            raise EgressToolError("search_provider_unconfigured")
        if set(args) - {"query", "language", "safesearch"}:
            raise EgressToolError("invalid_search_args")
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise EgressToolError("search_query_required")
        query = query.strip()
        if len(query) > self.config.max_query_chars:
            raise EgressToolError("search_query_too_long")
        language = args.get("language", "all")
        if not isinstance(language, str) or _LANGUAGE.fullmatch(language) is None:
            raise EgressToolError("invalid_search_language")
        safesearch = args.get("safesearch", 1)
        if type(safesearch) is not int or safesearch not in {0, 1, 2}:
            raise EgressToolError("invalid_search_safesearch")

        parsed = urlsplit(self.config.search_endpoint)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params.update(
            q=query,
            format="json",
            language=language,
            safesearch=str(safesearch),
        )
        target = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(params), "")
        )
        response = await self._get(target, allowlist=allowlist)
        if not 200 <= response.status_code < 300:
            raise EgressToolError(
                "search_provider_http_error", usage=response.usage
            )
        content_type = self._media_type(response.headers)
        if content_type not in {"application/json", "application/x-json", "text/json"}:
            raise EgressToolError(
                "search_provider_content_type", usage=response.usage
            )
        try:
            payload = json.loads(self._decode_text(response.body, response.headers))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise EgressToolError(
                "search_provider_invalid_json", usage=response.usage
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise EgressToolError(
                "search_provider_invalid_schema", usage=response.usage
            )

        results: list[dict[str, str]] = []
        for raw in payload["results"]:
            if not isinstance(raw, dict):
                continue
            try:
                url = normalize_http_url(
                    raw.get("url"), max_chars=self.config.max_url_chars
                )
            except UnsafeEgressTarget:
                continue
            title = bounded_text(raw.get("title"), 500)
            snippet = bounded_text(
                raw.get("content") or raw.get("snippet"), 1_500
            )
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= self.config.max_search_results:
                break
        return (
            {
                "tool": "web.search",
                "ok": True,
                "query": query,
                "provider": "searxng_json",
                "results": results,
            },
            response.usage,
        )

    async def _fetch(
        self, tool_name: str, args: dict, *, allowlist: list[str]
    ) -> tuple[dict, EgressUsage]:
        if set(args) != {"url"}:
            raise EgressToolError("invalid_fetch_args")
        raw_url = args.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise EgressToolError("fetch_url_required")
        response = await self._get(raw_url, allowlist=allowlist)
        content_type = self._media_type(response.headers)
        result: dict = {
            "tool": tool_name,
            "ok": 200 <= response.status_code < 400,
            "status_code": response.status_code,
            "url": response.url,
            "redirects": list(response.history),
            "content_type": content_type or "application/octet-stream",
            "title": "",
            "text": "",
            "links": [],
        }
        if content_type in {"text/html", "application/xhtml+xml"}:
            decoded = self._decode_text(response.body, response.headers)
            title, text, links = extract_html(
                decoded,
                base_url=response.url,
                max_text_chars=self.config.max_text_chars,
                max_links=self.config.max_links,
                max_url_chars=self.config.max_url_chars,
            )
            result.update(title=title, text=text, links=links)
        elif content_type.startswith("text/") or content_type in {
            "application/json",
            "application/x-json",
        }:
            decoded = self._decode_text(response.body, response.headers)
            result["text"] = bounded_text(decoded, self.config.max_text_chars)
        else:
            result["binary_omitted"] = True
        return result, response.usage

    async def _get(self, url: str, *, allowlist: list[str]):
        try:
            return await self.http.get(url, allowlist=allowlist)
        except EgressFetchError as exc:
            raise EgressToolError(exc.code, usage=exc.usage) from exc

    @staticmethod
    def _media_type(headers: dict[str, str]) -> str:
        return headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @staticmethod
    def _decode_text(body: bytes, headers: dict[str, str]) -> str:
        content_type = headers.get("content-type", "")
        match = _CHARSET.search(content_type)
        charset = match.group(1) if match else "utf-8"
        try:
            codecs.lookup(charset)
        except LookupError:
            charset = "utf-8"
        return body.decode(charset, errors="replace")
