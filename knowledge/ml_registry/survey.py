"""Offline-reviewable literature retrieval for an ML campaign's technique pool."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass


class TechniquePoolError(ValueError):
    """A retrieved technique is not safe to admit to the campaign pool."""


@dataclass(frozen=True)
class RetrievedWork:
    id: str
    title: str
    source_url: str


@dataclass(frozen=True)
class RetrievalFailure:
    provider: str
    query: str
    reason: str


@dataclass(frozen=True)
class RetrievalBatch:
    works: tuple[RetrievedWork, ...] = ()
    failures: tuple[RetrievalFailure, ...] = ()


@dataclass(frozen=True)
class Technique:
    id: str
    title: str
    source_url: str
    proven_where: str
    how_it_differs: str
    mechanism: str

    @property
    def why_it_should_still_help(self) -> str:
        """The mechanism field under the transferability triple's longer name."""
        return self.mechanism


@dataclass(frozen=True)
class TechniquePool:
    campaign_id: str
    techniques: tuple[Technique, ...]
    failures: tuple[RetrievalFailure, ...]
    minimum_size: int = 10

    @property
    def complete(self) -> bool:
        return len(self.techniques) >= self.minimum_size

    def to_dict(self) -> dict[str, object]:
        if self.complete:
            status = "complete"
        elif self.failures and not self.techniques:
            status = "failed"
        else:
            status = "incomplete"
        return {
            "campaign_id": self.campaign_id,
            "status": status,
            "minimum_size": self.minimum_size,
            "techniques": [asdict(item) for item in self.techniques],
            "retrieval_failures": [asdict(item) for item in self.failures],
        }


JsonFetcher = Callable[[str], Mapping[str, object]]
TransferabilityAnnotator = Callable[[RetrievedWork], Mapping[str, object]]


def _fetch_json(url: str) -> Mapping[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "praxis-survey/1"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed provider URL
        value = json.loads(response.read())
    if not isinstance(value, Mapping):
        raise ValueError("OpenAlex response must be a JSON object")
    return value


class OpenAlexClient:
    """Small OpenAlex works client with failures represented as data, not empty success."""

    endpoint = "https://api.openalex.org/works"

    def __init__(self, fetch_json: JsonFetcher = _fetch_json) -> None:
        self._fetch_json = fetch_json

    def search(self, query: str, *, per_page: int = 50) -> RetrievalBatch:
        cleaned_query = query.strip()
        if not cleaned_query:
            return RetrievalBatch(failures=(RetrievalFailure(
                "openalex", query, "query must not be empty",
            ),))
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        url = f"{self.endpoint}?{urllib.parse.urlencode({
            'search': cleaned_query, 'per-page': per_page,
        })}"
        try:
            payload = self._fetch_json(url)
            works = self._parse_works(payload)
        except (OSError, TimeoutError, TypeError, ValueError, urllib.error.URLError) as exc:
            return RetrievalBatch(failures=(RetrievalFailure(
                "openalex", cleaned_query, f"{type(exc).__name__}: {exc}",
            ),))
        return RetrievalBatch(works=works)

    @staticmethod
    def _parse_works(payload: Mapping[str, object]) -> tuple[RetrievedWork, ...]:
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("OpenAlex response has no results list")
        works: list[RetrievedWork] = []
        for raw in results:
            if not isinstance(raw, Mapping):
                continue
            work_id = _required_text(raw, "id", "OpenAlex work")
            title = _required_text(raw, "display_name", work_id)
            location = raw.get("primary_location")
            source_url = work_id
            if isinstance(location, Mapping):
                candidate_url = location.get("landing_page_url")
                if isinstance(candidate_url, str) and candidate_url.strip():
                    source_url = candidate_url.strip()
            works.append(RetrievedWork(work_id, title, source_url))
        return tuple(works)


def _required_text(value: Mapping[str, object], field: str, identity: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise TechniquePoolError(f"technique {identity!r} requires non-empty {field}")
    return item.strip()


def load_technique_pool(
    campaign_id: str,
    entries: Sequence[Mapping[str, object]],
    *,
    failures: Sequence[RetrievalFailure] = (),
    minimum_size: int = 10,
) -> TechniquePool:
    """Validate the transferability triple before an entry enters the pool."""
    if not campaign_id.strip():
        raise TechniquePoolError("campaign_id must not be empty")
    if minimum_size < 1:
        raise TechniquePoolError("minimum_size must be positive")
    techniques: list[Technique] = []
    seen: set[str] = set()
    for raw in entries:
        identity = _required_text(raw, "id", "<unknown>")
        if identity in seen:
            continue
        techniques.append(Technique(
            id=identity,
            title=_required_text(raw, "title", identity),
            source_url=_required_text(raw, "source_url", identity),
            proven_where=_required_text(raw, "proven_where", identity),
            how_it_differs=_required_text(raw, "how_it_differs", identity),
            mechanism=_required_text(raw, "mechanism", identity),
        ))
        seen.add(identity)
    return TechniquePool(campaign_id.strip(), tuple(techniques), tuple(failures), minimum_size)


def survey_campaign(
    campaign_id: str,
    queries: Sequence[str],
    client: OpenAlexClient,
    annotate: TransferabilityAnnotator,
    *,
    minimum_size: int = 10,
) -> TechniquePool:
    """Retrieve once before a campaign and produce its reviewable technique-pool artifact."""
    entries: list[Mapping[str, object]] = []
    failures: list[RetrievalFailure] = []
    seen: set[str] = set()
    for query in queries:
        batch = client.search(query)
        failures.extend(batch.failures)
        for work in batch.works:
            if work.id in seen:
                continue
            triple = annotate(work)
            entries.append({
                **triple,
                "id": work.id,
                "title": work.title,
                "source_url": work.source_url,
            })
            seen.add(work.id)
    return load_technique_pool(
        campaign_id, entries, failures=failures, minimum_size=minimum_size,
    )
