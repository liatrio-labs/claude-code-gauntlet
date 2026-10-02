"""Semantic forge queues and the shared consumer factory installer."""

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Literal, TypeVar

from gauntlet.forge import (
    GitLab,
    JsonFetch,
    Platform,
    PostRequest,
    PostResult,
    ReviewTarget,
)

Method = Literal["ensure_available", "diff", "review_entries", "diff_refs", "submit"]
Reply = TypeVar("Reply")
Submissions = (
    Iterable[PostResult | Exception] | Mapping[str, Iterable[PostResult | Exception]]
)


@dataclass(frozen=True, slots=True)
class ForgeCall:
    method: Method
    target: ReviewTarget | None = None
    request: PostRequest | None = None


def _reply(queue: list[Reply | Exception], method: Method) -> Reply:
    if not queue:
        raise AssertionError(f"Unexpected {method} call: configured queue exhausted")
    result = queue.pop(0)
    if isinstance(result, Exception):
        raise result
    return result


class FakeForge:
    def __init__(
        self,
        platform: Platform = "github",
        *,
        availability: Iterable[Exception | None] | None = None,
        diffs: Iterable[tuple[str, str, int] | Exception] = (),
        entries: Iterable[JsonFetch | Exception] | None = None,
        submissions: Submissions | None = None,
    ) -> None:
        self.platform = platform
        self.calls: list[ForgeCall] = []
        self._availability = list(availability) if availability is not None else None
        self._diffs = list(diffs)
        self._entries = list(entries) if entries is not None else None
        self._submissions: (
            dict[str, list[PostResult | Exception]]
            | list[PostResult | Exception]
            | None
        )
        if isinstance(submissions, Mapping):
            self._submissions = {
                surface: list(replies) for surface, replies in submissions.items()
            }
        elif submissions is not None:
            self._submissions = list(submissions)
        else:
            self._submissions = None

    def ensure_available(self) -> None:
        self.calls.append(ForgeCall("ensure_available"))
        if self._availability is not None:
            _reply(self._availability, "ensure_available")

    def diff(self, target: ReviewTarget) -> tuple[str, str, int]:
        self.calls.append(ForgeCall("diff", target=target))
        return _reply(self._diffs, "diff")

    def review_entries(self, target: ReviewTarget) -> JsonFetch:
        self.calls.append(ForgeCall("review_entries", target=target))
        return (
            _reply(self._entries, "review_entries")
            if self._entries is not None
            else JsonFetch([], None)
        )

    def submit(self, request: PostRequest) -> PostResult:
        self.calls.append(
            ForgeCall(
                "submit",
                request=replace(request, payload=deepcopy(dict(request.payload))),
            )
        )
        if isinstance(self._submissions, dict):
            surface = request.endpoint.rsplit("/", 1)[-1]
            if surface not in self._submissions:
                raise AssertionError(f"Unexpected submit surface: {surface}")
            return _reply(self._submissions[surface], "submit")
        return (
            _reply(self._submissions, "submit")
            if self._submissions is not None
            else PostResult({}, None, None)
        )


class FakeGitLab(FakeForge, GitLab):
    def __init__(
        self,
        *,
        refs: Iterable[JsonFetch | Exception] = (),
        availability: Iterable[Exception | None] | None = None,
        diffs: Iterable[tuple[str, str, int] | Exception] = (),
        entries: Iterable[JsonFetch | Exception] | None = None,
        submissions: Submissions | None = None,
    ) -> None:
        super().__init__(
            "gitlab",
            availability=availability,
            diffs=diffs,
            entries=entries,
            submissions=submissions,
        )
        self._refs = list(refs)

    def diff_refs(self, target: ReviewTarget) -> JsonFetch:
        self.calls.append(ForgeCall("diff_refs", target=target))
        return _reply(self._refs, "diff_refs")


class FakeForgeFactory:
    def __init__(self) -> None:
        self.calls: list[Platform] = []
        self._forges: dict[Platform, FakeForge] = {}

    def configure(self, fake: FakeForge) -> FakeForge:
        self._forges[fake.platform] = fake
        return fake

    def __call__(self, platform: Platform) -> FakeForge:
        self.calls.append(platform)
        if platform not in self._forges:
            raise AssertionError(f"No fake configured for {platform}")
        return self._forges[platform]


def install_forge_factory(monkeypatch, module: ModuleType) -> FakeForgeFactory:
    factory = FakeForgeFactory()
    # The fixture must also serve consumers whose factory attribute is absent.
    monkeypatch.setattr(module, "make_forge", factory, raising=False)
    return factory
