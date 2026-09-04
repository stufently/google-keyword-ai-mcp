import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import anyio
import httpx
from pydantic import ValidationError

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import (
    ApiError,
    NetworkError,
    ProviderUnavailableError,
    RateLimitError,
)
from google_keyword_ai.http import request_with_retries
from google_keyword_ai.providers.trends.models import (
    GeoInterest,
    RelatedQueries,
    RelatedQuery,
    TrendPoint,
    TrendsResult,
    build_normalization_scope,
)
from google_keyword_ai.ratelimit import AsyncRateLimiter

WARMUP_URL = "https://trends.google.com/_/TrendsUi/data/batchexecute"
EXPLORE_URL = "https://trends.google.com/trends/api/explore"
WIDGETDATA_URL = "https://trends.google.com/trends/api/widgetdata"
REFERER = "https://trends.google.com/trends/explore"
WIDGET_PATHS = {
    "TIMESERIES": "multiline",
    "GEO_MAP": "comparedgeo",
    "RELATED_QUERIES": "relatedsearches",
    "RELATED_TOPICS": "relatedsearches",
}

# Only these are fetched, in this order. RELATED_TOPICS has a path but no
# consumer: requesting it would spend one more call against a rate-limited
# unofficial endpoint, plus a pacing pause, and then discard the answer. Add it
# here on the day something reads topics.
CONSUMED_WIDGETS: tuple[str, ...] = ("TIMESERIES", "GEO_MAP", "RELATED_QUERIES")


def strip_prefix(payload: str) -> str:
    stripped = payload.lstrip()
    if stripped.startswith(")]}'"):
        stripped = stripped[4:]
    object_start = stripped.find("{")
    if object_start < 0:
        raise ApiError("Google Trends returned a response without a JSON object.")
    return stripped[object_start:]


def _decode(payload: str) -> dict[str, object]:
    try:
        decoded = json.loads(strip_prefix(payload))
    except json.JSONDecodeError as exc:
        raise ApiError("Google Trends returned invalid JSON.") from exc
    if not isinstance(decoded, dict):
        raise ApiError("Google Trends returned an unexpected response shape.")
    return cast(dict[str, object], decoded)


def parse_explore(payload: str) -> dict[str, dict[str, object]]:
    decoded = _decode(payload)
    widgets = decoded.get("widgets")
    if not isinstance(widgets, list):
        raise ApiError("Google Trends explore response has no widgets.")

    parsed: dict[str, dict[str, object]] = {}
    for widget in widgets:
        if not isinstance(widget, dict) or not isinstance(widget.get("id"), str):
            raise ApiError("Google Trends explore response contains an invalid widget.")
        typed_widget = cast(dict[str, object], widget)
        parsed[cast(str, widget["id"])] = typed_widget
    return parsed


def _default_node(payload: str) -> dict[str, object]:
    decoded = _decode(payload)
    default = decoded.get("default")
    if not isinstance(default, dict):
        raise ApiError("Google Trends widget response has no default node.")
    return cast(dict[str, object], default)


def _list_node(parent: dict[str, object], name: str) -> list[object]:
    value = parent.get(name)
    return cast(list[object], value) if isinstance(value, list) else []


def parse_timeline(payload: str) -> list[TrendPoint]:
    points: list[TrendPoint] = []
    try:
        for raw in _list_node(_default_node(payload), "timelineData"):
            if not isinstance(raw, dict):
                raise ValueError("timeline row is not an object")
            points.append(
                TrendPoint(
                    timestamp=datetime.fromtimestamp(int(raw["time"]), UTC),
                    formatted_time=raw["formattedTime"],
                    values=raw["value"],
                    has_data=raw.get("hasData", []),
                )
            )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ApiError("Google Trends timeline response is invalid.") from exc
    return points


def parse_geo(payload: str) -> list[GeoInterest]:
    rows: list[GeoInterest] = []
    try:
        for raw in _list_node(_default_node(payload), "geoMapData"):
            if not isinstance(raw, dict):
                raise ValueError("geo row is not an object")
            raw_has_data = raw.get("hasData")
            if isinstance(raw_has_data, list) and not any(raw_has_data):
                continue
            rows.append(
                GeoInterest(
                    geo_code=raw["geoCode"],
                    geo_name=raw["geoName"],
                    values=raw["value"],
                    has_data=raw.get("hasData", []),
                )
            )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ApiError("Google Trends geography response is invalid.") from exc
    return rows


def _parse_related_rows(rows: list[object]) -> list[RelatedQuery]:
    parsed: list[RelatedQuery] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("related query row is not an object")
        parsed.append(
            RelatedQuery(
                query=raw["query"],
                value=raw["value"],
                formatted_value=raw["formattedValue"],
                has_data=raw.get("hasData"),
            )
        )
    return parsed


def parse_related(payload: str) -> RelatedQueries:
    ranked_lists = _list_node(_default_node(payload), "rankedList")

    def rows_at(index: int) -> list[object]:
        if index >= len(ranked_lists) or not isinstance(ranked_lists[index], dict):
            return []
        return _list_node(cast(dict[str, object], ranked_lists[index]), "rankedKeyword")

    try:
        return RelatedQueries(
            top=_parse_related_rows(rows_at(0)),
            rising=_parse_related_rows(rows_at(1)),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ApiError("Google Trends related queries response is invalid.") from exc


class UnofficialTrendsClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        rate_limiter: AsyncRateLimiter,
    ) -> None:
        self._settings = settings
        self._client = client
        self._rate_limiter = rate_limiter
        self._warmed_up = False
        self._consecutive_failures = 0
        self._circuit_open = False
        self.warnings: list[str] = []
        # How the last fetch's widgets went. The result alone cannot say: a
        # keyword Google has no interest data for and a keyword whose widgets
        # were all refused both come back empty, and only these counters
        # separate them.
        self.widgets_attempted = 0
        self.widgets_failed = 0
        self._client.headers["User-Agent"] = settings.http_user_agent
        self._client.headers["Accept-Language"] = settings.default_language
        self._client.headers["Referer"] = REFERER

    async def warm_up(self) -> None:
        if self._warmed_up:
            return
        await self._rate_limiter.acquire()
        try:
            response = await self._client.get(WARMUP_URL)
        except httpx.TransportError as exc:
            raise NetworkError(
                f"Google Trends session warm-up failed: {exc}",
                {"status_code": None, "url": WARMUP_URL},
            ) from exc
        if response.status_code == 405 or response.is_success:
            self._warmed_up = True
            return
        details = {"status_code": response.status_code, "url": WARMUP_URL}
        if response.status_code == 429:
            raise RateLimitError("Google Trends session warm-up was rate limited.", details)
        raise ApiError(
            f"Google Trends session warm-up failed with status {response.status_code}.",
            details,
        )

    async def _get(self, url: str, params: dict[str, str]) -> httpx.Response:
        await self._rate_limiter.acquire()
        return await request_with_retries(
            self._client,
            "GET",
            url,
            params=params,
            settings=self._settings,
        )

    def all_widgets_failed(self) -> bool:
        """Say whether every widget the last fetch asked for was refused.

        Widgets `explore` never offered are not asked for and are not counted:
        their absence is Google's answer, not a failure to reach it.
        """
        return self.widgets_attempted > 0 and self.widgets_failed == self.widgets_attempted

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._settings.trends_circuit_breaker_failures:
            self._circuit_open = True

    async def fetch(
        self,
        keywords: Sequence[str],
        *,
        geo: str,
        timeframe: str,
        hl: str,
    ) -> TrendsResult:
        if self._circuit_open:
            raise ProviderUnavailableError("Google Trends circuit breaker is open.")

        self.warnings = []
        self.widgets_attempted = 0
        self.widgets_failed = 0
        keyword_list = list(keywords)
        self._client.headers["Accept-Language"] = hl
        try:
            await self.warm_up()
            explore_request = {
                "comparisonItem": [
                    {"keyword": keyword, "geo": geo, "time": timeframe} for keyword in keyword_list
                ],
                "category": 0,
                "property": "",
            }
            explore_response = await self._get(
                EXPLORE_URL,
                {
                    "hl": hl,
                    "tz": str(self._settings.trends_timezone_minutes),
                    "req": json.dumps(
                        explore_request,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            )
            widgets = parse_explore(explore_response.text)
        except (RateLimitError, NetworkError, ApiError, ProviderUnavailableError):
            self._record_failure()
            raise

        timeline: list[TrendPoint] = []
        geo_interest: list[GeoInterest] = []
        related = RelatedQueries()
        requested_widget = False
        for widget_id in CONSUMED_WIDGETS:
            widget = widgets.get(widget_id)
            if widget is None:
                per_keyword = sorted(name for name in widgets if name.startswith(f"{widget_id}_"))
                if per_keyword:
                    # A comparison splits some widgets one per keyword and
                    # suffixes their ids, so the plain name is simply not there.
                    # Silence would read as "Google has none of this", when in
                    # fact it has one set per keyword -- and they cannot be
                    # merged into the single list this result carries, because
                    # each is normalised inside its own widget. Comparing values
                    # across them would be exactly what `normalization_scope`
                    # exists to prevent.
                    self.warnings.append(
                        f"{widget_id} came back once per keyword "
                        f"({', '.join(per_keyword)}); a comparison normalises each "
                        "separately, so they are not merged into one list and are "
                        "not reported here."
                    )
                continue
            path = WIDGET_PATHS[widget_id]
            if requested_widget:
                await anyio.sleep(self._settings.trends_pacing_seconds)
            requested_widget = True
            self.widgets_attempted += 1
            request = widget.get("request")
            token = widget.get("token")
            if not isinstance(request, dict) or not isinstance(token, str):
                self.widgets_failed += 1
                self.warnings.append(f"{widget_id}: invalid widget metadata")
                continue
            try:
                response = await self._get(
                    f"{WIDGETDATA_URL}/{path}",
                    {
                        "hl": hl,
                        "tz": str(self._settings.trends_timezone_minutes),
                        "req": json.dumps(
                            request,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        "token": token,
                    },
                )
                if widget_id == "TIMESERIES":
                    timeline = parse_timeline(response.text)
                elif widget_id == "GEO_MAP":
                    geo_interest = parse_geo(response.text)
                elif widget_id == "RELATED_QUERIES":
                    related = parse_related(response.text)
            except (RateLimitError, NetworkError, ApiError, ProviderUnavailableError) as exc:
                self.widgets_failed += 1
                self.warnings.append(f"{widget_id}: {exc}")

        if self.all_widgets_failed():
            # Nothing came back, so the request failed as surely as a dead
            # `explore` -- and the breaker exists to stop asking a blocked
            # endpoint. Counting only the earlier stages let a run whose every
            # widget was refused reset the counter and go again, which is the
            # opposite of what the breaker is for.
            self._record_failure()
        else:
            self._consecutive_failures = 0
        return TrendsResult(
            keywords=keyword_list,
            geo=geo,
            timeframe=timeframe,
            normalization_scope=build_normalization_scope(
                keyword_list,
                geo=geo,
                timeframe=timeframe,
                hl=hl,
            ),
            timeline=timeline,
            geo_interest=geo_interest,
            related=related,
            retrieved_at=datetime.now(UTC),
            source=EXPLORE_URL,
        )
