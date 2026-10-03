"""Web search, page reading and weather. No API keys required."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from bagley.tools import ToolContext, ToolError, tool

USER_AGENT = (
    "Mozilla/5.0 (compatible; BagleyAssistant/0.1; +https://github.com/H4ch1Net/bagley-assistant)"
)
MAX_PAGE_BYTES = 3_000_000


async def check_url(url: str, allow_private: bool) -> list[str]:
    """Validate a URL and return the IP addresses it resolves to (empty if private hosts are
    allowed). Raises ``ToolError`` for non-http(s) URLs and private or local addresses."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("Only http(s) URLs with a host name are supported.")
    if allow_private:
        return []
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname,
            parts.port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ToolError(f"Couldn't resolve host {parts.hostname}.") from exc
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not ip.is_global:
            raise ToolError(
                f"{parts.hostname} points to a private or local address. "
                "Set BAGLEY_ALLOW_PRIVATE_URLS=true to allow this."
            )
        ips.append(str(ip))
    return ips


def _pinned(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Rewrite ``url`` to connect to the already-validated ``ip``, so a second DNS lookup
    (DNS rebinding) can't redirect the request to a private address. TLS still verifies the
    certificate against the original host name."""
    parts = urlsplit(url)
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{parts.port}" if parts.port else host
    target = parts._replace(netloc=netloc).geturl()
    host_header = parts.hostname or ""
    if parts.port:
        host_header += f":{parts.port}"
    extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
    return target, {"Host": host_header}, extensions


async def fetch_limited(
    ctx: ToolContext, url: str, *, max_bytes: int = MAX_PAGE_BYTES, timeout: float = 20.0
) -> tuple[str, httpx.Response]:
    """GET ``url`` following redirects, re-checking every hop against the private-network rule
    and reading at most ``max_bytes``. Returns the final URL and a fully read response."""
    for _ in range(6):
        ips = await check_url(url, ctx.config.allow_private_urls)
        target, headers, extensions = _pinned(url, ips[0]) if ips else (url, {}, {})
        headers["User-Agent"] = USER_AGENT
        async with ctx.http.stream(
            "GET",
            target,
            headers=headers,
            extensions=extensions,
            follow_redirects=False,
            timeout=timeout,
        ) as resp:
            if resp.is_redirect and "location" in resp.headers:
                url = urljoin(url, resp.headers["location"])
                continue
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise ToolError("Page is too large to read.")
            return url, httpx.Response(resp.status_code, headers=resp.headers, content=bytes(body))
    raise ToolError("Too many redirects.")


class _TextExtractor(HTMLParser):
    SKIP = frozenset(
        {"script", "style", "noscript", "svg", "nav", "footer", "form", "iframe", "template"}
    )
    BLOCK = frozenset({
        "p", "div", "section", "article", "br", "li", "ul", "ol", "tr", "table", "header",
        "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "main", "aside", "dd", "dt",
    })  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3") and not self._skip:
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "li" and not self._skip:
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return " ".join(parser.title.split()), parser.text()


@tool(category="web", summary="Read {url}", timeout=40)
async def fetch_webpage(
    ctx: ToolContext,
    url: Annotated[str, "Full http(s) URL of the page"],
    max_chars: Annotated[int, "Maximum characters of text to return"] = 6000,
) -> dict[str, Any]:
    """Download a web page and return its readable text. Use it to read links the user shares or
    to dig into a search result."""
    try:
        final_url, resp = await fetch_limited(ctx, url)
    except httpx.HTTPError as exc:
        raise ToolError(f"Couldn't fetch {url}: {exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        raise ToolError(f"{url} returned HTTP {resp.status_code}.")
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype or resp.text.lstrip().startswith("<"):
        title, text = html_to_text(resp.text)
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        title, text = "", resp.text
    else:
        raise ToolError(f"Can't read content of type {ctype or 'unknown'}.")
    max_chars = max(500, min(max_chars, 20_000))
    truncated = len(text) > max_chars
    return {
        "url": final_url,
        "title": title,
        "text": text[:max_chars] + ("…" if truncated else ""),
        "truncated": truncated,
    }


_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?:.*?<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
    re.S,
)
_TAGS = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return " ".join(html.unescape(_TAGS.sub("", fragment or "")).split())


def parse_duckduckgo(markup: str, limit: int) -> list[dict[str, str]]:
    results = []
    for m in _DDG_RESULT.finditer(markup):
        href = html.unescape(m.group("href"))
        if "duckduckgo.com/l/" in href:
            href = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com/y.js" in href:  # Ads.
            continue
        results.append(
            {"title": _clean(m.group("title")), "url": href, "snippet": _clean(m.group("snippet"))}
        )
        if len(results) >= limit:
            break
    return results


@tool(category="web", summary="Search the web for “{query}”", timeout=30)
async def web_search(
    ctx: ToolContext,
    query: Annotated[str, "Search query"],
    max_results: Annotated[int, "Number of results (1-10)"] = 5,
) -> list[dict[str, str]]:
    """Search the web for current information. Returns titles, URLs and snippets. Follow up with
    fetch_webpage to read a result in full."""
    limit = max(1, min(max_results, 10))
    try:
        if ctx.config.searxng_url:
            resp = await ctx.http.get(
                f"{ctx.config.searxng_url}/search",
                params={"q": query, "format": "json"},
                timeout=20.0,
            )
            resp.raise_for_status()
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                }
                for r in resp.json().get("results", [])[:limit]
            ]
        else:
            resp = await ctx.http.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": USER_AGENT},
                timeout=20.0,
            )
            resp.raise_for_status()
            results = parse_duckduckgo(resp.text, limit)
    except httpx.HTTPError as exc:
        raise ToolError(
            f"Search failed: {exc.__class__.__name__}. Check your internet connection."
        ) from exc
    if not results:
        raise ToolError(
            "The search returned no results. DuckDuckGo may be rate limiting; "
            "set BAGLEY_SEARXNG_URL to use your own SearXNG instance."
        )
    return results


WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}  # fmt: skip


@tool(category="web", summary="Check the weather in {location}", timeout=30)
async def get_weather(
    ctx: ToolContext,
    location: Annotated[str, "City name, optionally with country, e.g. 'Paris, France'"],
    days: Annotated[int, "Days of forecast (1-7)"] = 3,
    units: Literal["metric", "imperial"] = "metric",
) -> dict[str, Any]:
    """Get current weather and a short daily forecast for a place (Open-Meteo)."""
    name, _, hint = (part.strip() for part in location.partition(","))
    try:
        geo = await ctx.http.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 5, "language": "en", "format": "json"},
            timeout=15.0,
        )
        geo.raise_for_status()
        places = geo.json().get("results") or []
        if not places:
            raise ToolError(f"Couldn't find a place called '{location}'.")
        place = places[0]
        if hint:
            needle = hint.lower()
            place = next(
                (
                    p
                    for p in places
                    if needle
                    in f"{p.get('country', '')} {p.get('country_code', '')} {p.get('admin1', '')}".lower()
                ),
                place,
            )
        imperial = units == "imperial"
        forecast = await ctx.http.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "weather_code,wind_speed_10m,precipitation",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": max(1, min(days, 7)),
                "temperature_unit": "fahrenheit" if imperial else "celsius",
                "wind_speed_unit": "mph" if imperial else "kmh",
            },
            timeout=15.0,
        )
        forecast.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolError(f"Weather service unavailable: {exc.__class__.__name__}") from exc
    data = forecast.json()
    cur = data.get("current", {})
    daily = data.get("daily", {})
    t_unit = "°F" if imperial else "°C"
    w_unit = "mph" if imperial else "km/h"
    return {
        "location": ", ".join(
            filter(None, [place.get("name"), place.get("admin1"), place.get("country")])
        ),
        "local_time": cur.get("time"),
        "current": {
            "conditions": WMO_CODES.get(cur.get("weather_code"), "unknown"),
            "temperature": f"{cur.get('temperature_2m')}{t_unit}",
            "feels_like": f"{cur.get('apparent_temperature')}{t_unit}",
            "humidity": f"{cur.get('relative_humidity_2m')}%",
            "wind": f"{cur.get('wind_speed_10m')} {w_unit}",
            "precipitation": f"{cur.get('precipitation')} {'in' if imperial else 'mm'}",
        },
        "forecast": [
            {
                "date": date,
                "conditions": WMO_CODES.get(code, "unknown"),
                "high": f"{hi}{t_unit}",
                "low": f"{lo}{t_unit}",
                "chance_of_rain": f"{rain}%" if rain is not None else "n/a",
            }
            for date, code, hi, lo, rain in zip(
                daily.get("time", []),
                daily.get("weather_code", []),
                daily.get("temperature_2m_max", []),
                daily.get("temperature_2m_min", []),
                daily.get("precipitation_probability_max", [])
                or [None] * len(daily.get("time", [])),
                strict=False,
            )
        ],
    }
