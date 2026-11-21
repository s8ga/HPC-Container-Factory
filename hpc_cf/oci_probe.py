"""Host-side OCI Distribution v2 buildcache probe.

Spack's ``spack buildcache check --mirror-url oci://...`` is non-functional in
1.2.x (returns rc=1 regardless of registry contents). Spack pushes each
concrete binary under the deterministic tag ``<name>-<version>-<daghash>.spack``,
so a pure host-side HTTP/HTTPS ``HEAD /v2/<repo>/manifests/<tag>`` pass verifies
registry coverage in seconds using Python stdlib only.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CHALLENGE_KV_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


@dataclass(frozen=True)
class OciTarget:
    """Parsed OCI buildcache registry coordinates."""

    http_scheme: str
    registry: str
    repository: str


@dataclass(frozen=True)
class OciProbeReport:
    """Result of probing ``spack.lock`` tags against an OCI buildcache."""

    mirror_url: str
    total: int
    present: int
    missing: int
    present_tags: tuple[str, ...]
    missing_tags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mirror_url": self.mirror_url,
            "total": self.total,
            "present": self.present,
            "missing": self.missing,
            "present_tags": list(self.present_tags),
            "missing_tags": list(self.missing_tags),
        }


def parse_oci_url(url: str) -> OciTarget:
    """Parse ``oci://registry/repo`` or ``oci+http://registry/repo``."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme == "oci":
        http_scheme = "https"
    elif parsed.scheme == "oci+http":
        http_scheme = "http"
    else:
        raise ValueError(
            f"Unsupported OCI buildcache URL scheme in {url!r}; "
            "expected oci:// or oci+http://"
        )
    registry = parsed.netloc
    repository = parsed.path.strip("/")
    if not registry or not repository:
        raise ValueError(
            f"OCI buildcache URL must include host and repository path: {url!r}"
        )
    return OciTarget(
        http_scheme=http_scheme,
        registry=registry,
        repository=repository,
    )


def expected_oci_tags_from_lock(
    lock_path: Path,
    *,
    exclude_specs: Sequence[str] = (),
) -> list[str]:
    """Derive deterministic ``<name>-<version>-<dag_hash>.spack`` tags from spack.lock."""
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    concrete = payload.get("concrete_specs")
    if not isinstance(concrete, dict) or not concrete:
        raise ValueError(f"spack.lock has no concrete_specs: {lock_path}")

    excluded = {str(name) for name in exclude_specs if name}
    tags: list[str] = []
    seen: set[str] = set()
    for dag_hash, node in concrete.items():
        if not isinstance(node, dict):
            continue
        if node.get("external"):
            continue
        name = node.get("name")
        version = node.get("version")
        if not name or not version:
            continue
        if str(name) in excluded:
            continue
        tag = f"{name}-{version}-{dag_hash}.spack"
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _parse_bearer_challenge(header: str) -> dict[str, str]:
    if not header or not header.lower().startswith("bearer "):
        return {}
    return {m.group(1).lower(): m.group(2) for m in _CHALLENGE_KV_RE.finditer(header)}


class _OciRegistryClient:
    """Thread-safe OCI Distribution v2 client supporting Bearer & Basic auth."""

    def __init__(
        self,
        target: OciTarget,
        *,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.target = target
        self.username = username
        self.password = password
        self.timeout = timeout
        self._lock = threading.Lock()
        self._bearer_token: str | None = None

    def _basic_header(self) -> str | None:
        if self.username and self.password is not None:
            raw = f"{self.username}:{self.password}".encode()
            return "Basic " + base64.b64encode(raw).decode("ascii")
        return None

    def _fetch_bearer_token(self, www_auth: str) -> str:
        params = _parse_bearer_challenge(www_auth)
        realm = params.get("realm")
        if not realm:
            raise RuntimeError(
                f"OCI registry returned 401 without Bearer realm: {www_auth!r}"
            )
        query_items: list[tuple[str, str]] = []
        if params.get("service"):
            query_items.append(("service", params["service"]))
        scope = params.get("scope") or f"repository:{self.target.repository}:pull"
        query_items.append(("scope", scope))
        sep = "&" if "?" in realm else "?"
        token_url = f"{realm}{sep}{urllib.parse.urlencode(query_items)}"

        headers: dict[str, str] = {"Accept": "application/json"}
        basic = self._basic_header()
        if basic:
            headers["Authorization"] = basic
        req = urllib.request.Request(token_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        token = body.get("token") or body.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"OCI token endpoint did not return a token: {realm}")
        return token

    def manifest_exists(self, tag: str) -> bool:
        """Return True if *tag* exists in the repository (404 -> False)."""
        url = (
            f"{self.target.http_scheme}://{self.target.registry}"
            f"/v2/{self.target.repository}/manifests/{tag}"
        )
        for attempt in range(2):
            headers = {"Accept": _MANIFEST_ACCEPT}
            with self._lock:
                token = self._bearer_token
            if token:
                headers["Authorization"] = f"Bearer {token}"
            elif attempt == 0:
                basic = self._basic_header()
                if basic:
                    headers["Authorization"] = basic

            req = urllib.request.Request(url, headers=headers, method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return 200 <= resp.status < 300
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return False
                if exc.code == 401 and attempt == 0:
                    www_auth = exc.headers.get("WWW-Authenticate", "") if exc.headers else ""
                    if www_auth.lower().startswith("bearer "):
                        with self._lock:
                            if self._bearer_token == token:
                                self._bearer_token = self._fetch_bearer_token(www_auth)
                        continue
                raise RuntimeError(
                    f"OCI manifest probe failed ({exc.code}) for {tag}: {exc.reason}"
                ) from exc
            except OSError as exc:
                raise RuntimeError(
                    f"OCI manifest probe network error for {tag}: {exc}"
                ) from exc
        raise RuntimeError(f"OCI authentication failed for {tag}")


def probe_oci_tags(
    *,
    mirror_url: str,
    tags: Sequence[str],
    username: str | None = None,
    password: str | None = None,
    concurrency: int = 16,
    timeout: float = 15.0,
) -> OciProbeReport:
    """Concurrently check which *tags* exist in *mirror_url*."""
    tag_list = list(tags)
    if not tag_list:
        return OciProbeReport(
            mirror_url=mirror_url,
            total=0,
            present=0,
            missing=0,
            present_tags=(),
            missing_tags=(),
        )
    target = parse_oci_url(mirror_url)
    client = _OciRegistryClient(
        target,
        username=username,
        password=password,
        timeout=timeout,
    )
    results: dict[str, bool] = {}
    workers = max(1, min(int(concurrency), len(tag_list)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.manifest_exists, tag): tag for tag in tag_list}
        for fut in as_completed(futures):
            tag = futures[fut]
            results[tag] = fut.result()

    present_tags = tuple(tag for tag in tag_list if results[tag])
    missing_tags = tuple(tag for tag in tag_list if not results[tag])
    return OciProbeReport(
        mirror_url=mirror_url,
        total=len(tag_list),
        present=len(present_tags),
        missing=len(missing_tags),
        present_tags=present_tags,
        missing_tags=missing_tags,
    )


def probe_oci_lock(
    *,
    lock_path: Path,
    mirror_url: str,
    exclude_specs: Sequence[str] = (),
    username: str | None = None,
    password: str | None = None,
    concurrency: int = 16,
    timeout: float = 15.0,
) -> OciProbeReport:
    """Probe all non-external, non-excluded specs in *lock_path* against *mirror_url*."""
    tags = expected_oci_tags_from_lock(lock_path, exclude_specs=exclude_specs)
    return probe_oci_tags(
        mirror_url=mirror_url,
        tags=tags,
        username=username,
        password=password,
        concurrency=concurrency,
        timeout=timeout,
    )
