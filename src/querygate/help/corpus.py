"""Load and search the packaged product guide without network or model access."""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from typing import Iterable

import yaml

from querygate.core.exceptions import NotFoundError, QueryValidationError
from querygate.help.models import GuideTopic

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.:-]*")


class GuideCorpus:
    def __init__(self, *, version: str, topics: Iterable[GuideTopic]) -> None:
        self.version = version
        self._topics = {topic.id: topic for topic in topics}
        if not self._topics:
            raise ValueError("The QueryGate guide corpus is empty.")

    @classmethod
    def load(cls) -> "GuideCorpus":
        content = resources.files("querygate.help.content")
        manifest = yaml.safe_load(content.joinpath("manifest.yaml").read_text()) or {}
        version = str(manifest.get("version", "")).strip()
        if not version:
            raise ValueError("The QueryGate guide manifest has no version.")

        topics: list[GuideTopic] = []
        seen: set[str] = set()
        for path in sorted(content.iterdir(), key=lambda entry: entry.name):
            if not path.name.endswith(".md"):
                continue
            raw = path.read_text()
            metadata, body = _split_front_matter(raw, path.name)
            topic = GuideTopic(
                id=metadata["id"],
                title=metadata["title"],
                summary=metadata["summary"],
                tags=metadata.get("tags", []),
                next_actions=metadata.get("next_actions", []),
                body=body.strip(),
                source=f"querygate.help.content/{path.name}",
            )
            if topic.id in seen:
                raise ValueError(f"Duplicate QueryGate guide topic id: {topic.id!r}")
            seen.add(topic.id)
            topics.append(topic)
        return cls(version=version, topics=topics)

    def get(self, topic_id: str) -> GuideTopic:
        topic = self._topics.get(topic_id)
        if topic is None:
            raise NotFoundError(f"Unknown guide topic: {topic_id!r}")
        return topic

    def topic_ids(self) -> list[str]:
        return sorted(self._topics)

    def search(self, query: str, limit: int = 5) -> list[tuple[GuideTopic, float]]:
        normalized = query.strip().lower()
        tokens = _tokens(normalized)
        if not tokens:
            raise QueryValidationError("Guide search needs at least one letter or number.")
        if not 1 <= limit <= 10:
            raise QueryValidationError("Guide search limit must be between 1 and 10.")

        ranked: list[tuple[GuideTopic, float]] = []
        for topic in self._topics.values():
            score = _score(topic, normalized, tokens)
            if score > 0:
                ranked.append((topic, round(score, 3)))
        ranked.sort(key=lambda item: (-item[1], item[0].id))
        return ranked[:limit]


def _split_front_matter(raw: str, source: str) -> tuple[dict, str]:
    if not raw.startswith("---\n"):
        raise ValueError(f"Guide topic {source!r} has no YAML front matter.")
    try:
        metadata_raw, body = raw[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError(f"Guide topic {source!r} has malformed YAML front matter.") from exc
    metadata = yaml.safe_load(metadata_raw) or {}
    for required in ("id", "title", "summary"):
        if not metadata.get(required):
            raise ValueError(f"Guide topic {source!r} is missing {required!r}.")
    return metadata, body


def _tokens(value: str) -> list[str]:
    return _TOKEN.findall(value.lower())


def _score(topic: GuideTopic, query: str, tokens: list[str]) -> float:
    topic_id = topic.id.lower()
    title = topic.title.lower()
    summary = topic.summary.lower()
    body = topic.body.lower()
    tags = [tag.lower() for tag in topic.tags]
    score = 0.0

    if query == topic_id or query == title:
        score += 50
    elif query in title:
        score += 18
    elif query in summary:
        score += 8

    matched = 0
    title_tokens = _tokens(title)
    body_tokens = _tokens(body)
    summary_tokens = _tokens(summary)
    for token in tokens:
        token_score = 0.0
        if token in topic_id:
            token_score += 7
        if token in title_tokens:
            token_score += 6
        if token in tags:
            token_score += 5
        token_score += min(summary_tokens.count(token), 2) * 2.5
        token_score += min(body_tokens.count(token), 5) * 0.5
        if token_score:
            matched += 1
            score += token_score
    score += 4 * (matched / len(tokens))
    if matched == len(tokens):
        score += 3
    return score


@lru_cache(maxsize=1)
def get_guide_corpus() -> GuideCorpus:
    return GuideCorpus.load()
