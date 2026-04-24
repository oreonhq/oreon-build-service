"""Pydantic request/response schemas for API."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """List response with total count for pagination."""
    items: List[T]
    total: int


# --- Account ---
class RoleNameSchema(str, Enum):
    admin = "admin"
    maintainer = "maintainer"


class AccountCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8)
    role: RoleNameSchema


class AccountUpdate(BaseModel):
    password: Optional[str] = Field(None, min_length=8)
    role: Optional[RoleNameSchema] = None
    is_active: Optional[bool] = None


class AccountResponse(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Release ---
class ReleaseChannelSchema(str, Enum):
    dev = "dev"
    testing = "testing"
    stable = "stable"


class ReleaseRepoCreate(BaseModel):
    name: str = Field(..., max_length=128)
    baseurl: str = Field(..., max_length=1024)
    priority: int = 0
    enabled: bool = True


class ReleaseCreate(BaseModel):
    releasename: str = Field(..., max_length=128)
    description: Optional[str] = None
    architectures: str = Field(..., description="Comma-separated, e.g. x86_64,aarch64")
    default_channel: ReleaseChannelSchema = ReleaseChannelSchema.dev
    default_repo_branch: str = Field(
        default="dev",
        max_length=32,
        description="R2 folder under releasename/ for built RPMs (usually dev); not a Git branch name",
    )
    base_repos: List[ReleaseRepoCreate] = []


class ReleaseUpdate(BaseModel):
    description: Optional[str] = None
    architectures: Optional[str] = None
    default_channel: Optional[ReleaseChannelSchema] = None
    default_repo_branch: Optional[str] = Field(None, max_length=32)
    is_active: Optional[bool] = None
    base_repos: Optional[List[ReleaseRepoCreate]] = None


class ReleaseRepoResponse(BaseModel):
    id: int
    name: str
    baseurl: str
    priority: int
    enabled: bool

    model_config = ConfigDict(from_attributes=True)


class ReleaseResponse(BaseModel):
    id: int
    releasename: str
    description: Optional[str] = None
    architectures: str
    default_channel: str
    default_repo_branch: str
    is_active: bool
    created_at: datetime
    base_repos: List[ReleaseRepoResponse] = []

    model_config = ConfigDict(from_attributes=True)


# --- Mock environment ---
# Mock ``--addrepo`` lines: additional (misc) + lookaside (bootstrap) combined cap.
_MOCK_REPO_MAX_COMBINED = 32


def _validate_mock_repo_url_list(
    v: object, *, allow_none: bool, per_field_cap: int
) -> Optional[List[str]]:
    if v is None:
        return None if allow_none else []
    if not isinstance(v, list):
        raise ValueError("repo URL list must be a list of strings")
    out: List[str] = []
    for item in v:
        s = str(item).strip()
        if not s:
            continue
        if len(s) > 2048:
            raise ValueError("repo URL exceeds 2048 characters")
        if len(out) >= per_field_cap:
            raise ValueError(f"at most {per_field_cap} URLs in this field")
        if not s.startswith(("http://", "https://")):
            raise ValueError("each repo URL must start with http:// or https://")
        out.append(s)
    return out


def _validate_mock_additional_repo_urls(v: object, *, allow_none: bool) -> Optional[List[str]]:
    return _validate_mock_repo_url_list(
        v, allow_none=allow_none, per_field_cap=_MOCK_REPO_MAX_COMBINED
    )


def _validate_optional_http_url(v: object, *, field_name: str) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if len(s) > 2048:
        raise ValueError(f"{field_name} exceeds 2048 characters")
    if not s.startswith(("http://", "https://")):
        raise ValueError(f"{field_name} must start with http:// or https://")
    return s


class MockEnvironmentCreate(BaseModel):
    release_id: int
    name: str = Field(..., max_length=128)
    config_name: str = Field(..., max_length=128, description="Base config name; at build time worker uses <config_name>-<arch>")
    config_content: Optional[str] = None
    additional_repo_urls: List[str] = Field(default_factory=list, description="Extra mock --addrepo URLs (before lookaside).")
    lookaside_repo_urls: List[str] = Field(
        default_factory=list,
        description="Bootstrap / Fedora lookaside; appended after additional_repo_urls for mock --addrepo.",
    )
    lookaside_gpg_key_url: Optional[str] = Field(
        None,
        max_length=2048,
        description="Optional URL to ASCII-armored RPM GPG key used by lookaside repo packages.",
    )
    is_available: bool = True
    priority: int = 0

    @field_validator("additional_repo_urls", mode="before")
    @classmethod
    def _repos_create_additional(cls, v: object) -> List[str]:
        out = _validate_mock_additional_repo_urls(v, allow_none=False)
        return list(out) if out is not None else []

    @field_validator("lookaside_repo_urls", mode="before")
    @classmethod
    def _repos_create_lookaside(cls, v: object) -> List[str]:
        out = _validate_mock_repo_url_list(
            v, allow_none=False, per_field_cap=_MOCK_REPO_MAX_COMBINED
        )
        return list(out) if out is not None else []

    @field_validator("lookaside_gpg_key_url", mode="before")
    @classmethod
    def _lookaside_key_url_create(cls, v: object) -> Optional[str]:
        return _validate_optional_http_url(v, field_name="lookaside_gpg_key_url")

    @model_validator(mode="after")
    def _combined_addrepo_limit(self) -> "MockEnvironmentCreate":
        a, b = self.additional_repo_urls, self.lookaside_repo_urls
        if len(a) + len(b) > _MOCK_REPO_MAX_COMBINED:
            raise ValueError(
                f"additional_repo_urls and lookaside_repo_urls combined may have at most {_MOCK_REPO_MAX_COMBINED} entries"
            )
        return self


class MockEnvironmentUpdate(BaseModel):
    name: Optional[str] = None
    config_name: Optional[str] = None
    config_content: Optional[str] = None
    additional_repo_urls: Optional[List[str]] = None
    lookaside_repo_urls: Optional[List[str]] = None
    lookaside_gpg_key_url: Optional[str] = None
    is_available: Optional[bool] = None
    priority: Optional[int] = None

    @field_validator("additional_repo_urls", mode="before")
    @classmethod
    def _repos_update_add(cls, v: object) -> Optional[List[str]]:
        if v is None:
            return None
        return _validate_mock_additional_repo_urls(v, allow_none=False)

    @field_validator("lookaside_repo_urls", mode="before")
    @classmethod
    def _repos_update_lookaside(cls, v: object) -> Optional[List[str]]:
        if v is None:
            return None
        return _validate_mock_repo_url_list(
            v, allow_none=False, per_field_cap=_MOCK_REPO_MAX_COMBINED
        )

    @field_validator("lookaside_gpg_key_url", mode="before")
    @classmethod
    def _lookaside_key_url_update(cls, v: object) -> Optional[str]:
        return _validate_optional_http_url(v, field_name="lookaside_gpg_key_url")


class MockEnvironmentResponse(BaseModel):
    id: int
    release_id: int
    name: str
    config_name: str
    architecture: Optional[str] = None
    additional_repo_urls: List[str] = Field(default_factory=list)
    lookaside_repo_urls: List[str] = Field(default_factory=list)
    lookaside_gpg_key_url: Optional[str] = None
    is_available: bool
    priority: int
    created_at: datetime
    config_content: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# --- Package ---
class SourceTypeSchema(str, Enum):
    url = "url"
    upload = "upload"
    scm = "scm"
    distgit = "distgit"


class NewPackageForBuild(BaseModel):
    """Create a new package with source and use it for the build."""
    name: str = Field(..., max_length=256, description="Package name")
    description: Optional[str] = None
    preferred_mock_environment_id: Optional[int] = Field(None, description="Mock env to use for rebuilds (must belong to build release)")
    github_repository_id: Optional[int] = Field(None, description="Numeric id from GitHub API (webhook matching)")
    github_html_url: Optional[str] = Field(None, max_length=512, description="https://github.com/org/repo for webhook matching")
    source_type: SourceTypeSchema
    source_url: Optional[str] = Field(None, max_length=1024, description="Primary URL (URL/DistGit/SCM)")
    source_urls: Optional[List[str]] = Field(None, description="Multiple URLs for 'url' source type")
    source_branch: Optional[str] = Field(None, max_length=128, description="Branch or ref (SCM/DistGit)")
    source_path: Optional[str] = Field(None, max_length=256, description="Path in repo (e.g. package name in DistGit)")
    version: Optional[str] = Field(None, max_length=64, description="Package version (default 0.0.1)")


class PackageCreate(BaseModel):
    name: str = Field(..., max_length=256)
    description: Optional[str] = None
    preferred_mock_environment_id: Optional[int] = None
    github_repository_id: Optional[int] = None
    github_html_url: Optional[str] = None


class PackageUpdate(BaseModel):
    description: Optional[str] = None
    preferred_mock_environment_id: Optional[int] = None
    github_repository_id: Optional[int] = None
    github_html_url: Optional[str] = None


class PackageResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    preferred_mock_environment_id: Optional[int] = None
    github_repository_id: Optional[int] = None
    github_html_url: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PackageVersionResponse(BaseModel):
    id: int
    package_id: int
    version: str
    release_tag: Optional[str] = None
    git_sha: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Build ---
class BuildStatusSchema(str, Enum):
    pending = "pending"
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"
    skipped = "skipped"


class BuildTrigger(BaseModel):
    """Trigger build for an existing package, or create a new package and build."""
    package_id: Optional[int] = Field(None, description="Existing package ID (omit if using new_package)")
    new_package: Optional[NewPackageForBuild] = Field(None, description="Create this package and build it")
    release_id: int = Field(..., description="Release to build for")
    architectures: List[str] = Field(
        default=["x86_64", "aarch64"],
        description="Architectures to build for; job per arch; no worker for arch = that job fails",
    )
    package_version_id: Optional[int] = None
    target_id: Optional[int] = None
    priority: int = 0
    skip_complete_arches: bool = Field(
        default=True,
        description=(
            "If true, do not queue an arch when a successful job already exists for this package, "
            "release, and resolved mock target. Missing arches still queue. Retries use POST /builds/jobs/{id}/retry."
        ),
    )

    @model_validator(mode="after")
    def require_package_or_new(self):
        if (self.package_id is None) == (self.new_package is None):
            raise ValueError("Provide exactly one of package_id or new_package")
        return self


class ChainBuildTrigger(BaseModel):
    """DistGit chain build (colon separates parallel groups; no colon means one package per stage)."""

    chain_spec: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description=(
            "Whitespace-separated NVRs or package names. Colon ``:`` separates groups that run in parallel; "
            "groups run in order. With no colons, each package is its own group (strictly sequential)."
        ),
    )
    release_id: int = Field(..., description="Release to build for")
    architectures: List[str] = Field(
        default=["x86_64", "aarch64"],
        description="Architectures to build for (same as single trigger)",
    )
    skip_complete_arches: bool = Field(
        default=True,
        description="If true, skip arches that already have a successful build for that package and target.",
    )
    preferred_mock_environment_id: Optional[int] = Field(
        None, description="Mock env for new distgit packages (must belong to the release)"
    )
    distgit_url: Optional[str] = Field(
        None,
        max_length=1024,
        description="Git URL for distgit spec repo (default oreonhq/rpm-specfiles)",
    )
    distgit_branch: Optional[str] = Field(None, max_length=128, description="Branch fragment in distgit URL")
    priority: int = 0


class ChainResumeRequest(BaseModel):
    """Re-submit the same chain as a new chain, skipping packages that already built successfully."""

    from_chain_id: int = Field(..., ge=1, description="Previous build chain id (failed, cancelled, or success)")


class BuildJobResponse(BaseModel):
    id: int
    package_id: int
    package_name: Optional[str] = None
    release_id: int
    release_name: Optional[str] = None
    target_id: Optional[int] = None
    architecture: Optional[str] = None
    package_version_id: Optional[int] = None
    status: str
    #: Badge-friendly status: ``success`` once mock finished, even if controller pipeline still running.
    display_status: str = ""
    #: Latest attempt controller pipeline: ``not_started`` | ``running`` | ``signed`` | ``failed``.
    signing_status: Optional[str] = None
    priority: int
    created_at: datetime
    completed_at: Optional[datetime] = None
    chain_id: Optional[int] = None
    chain_stage_index: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class ChainBuildTriggerResponse(BaseModel):
    chain_id: int
    chain_status: str
    stages: List[List[str]]
    chain_spec: str
    jobs: List[BuildJobResponse]


class CurrentFailureItem(BaseModel):
    """Latest job per package or release or arch slot is failed (ignores older jobs in that slot)."""

    id: int
    package_id: int
    package_name: str
    release_id: int
    release_name: str
    target_id: Optional[int] = None
    architecture: Optional[str] = None
    package_version_id: Optional[int] = None
    status: str
    priority: int
    created_at: datetime
    completed_at: Optional[datetime] = None
    last_attempt_id: Optional[int] = None
    last_error_message: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class BuildAttemptResponse(BaseModel):
    id: int
    build_job_id: int
    worker_id: Optional[int] = None
    attempt_number: int
    status: str
    #: Badge: ``success`` when mock succeeded, even if ``status`` is still ``running`` (upload to controller).
    display_status: str = ""
    mock_succeeded_at: Optional[datetime] = None
    #: Controller pipeline after mock (GPG signing runs on controller, not worker).
    signing_status: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    log_r2_key: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    worker_name: Optional[str] = None
    worker_last_heartbeat: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ArtifactResponse(BaseModel):
    id: int
    build_attempt_id: int
    kind: str
    filename: str
    r2_key: str
    size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None
     # True if the RPM was successfully GPG-signed on the worker
    is_signed: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


# --- Worker ---
class WorkerStateSchema(str, Enum):
    idle = "idle"
    busy = "busy"
    unhealthy = "unhealthy"
    offline = "offline"
    draining = "draining"


class WorkerEnroll(BaseModel):
    name: str = Field(..., max_length=128)
    enrollment_token: str
    architecture: Optional[str] = None


class WorkerResponse(BaseModel):
    id: int
    name: str
    state: str
    architecture: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    enrolled_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Promotion ---
class PromoteRequest(BaseModel):
    release_id: int
    from_channel: str = Field(..., max_length=32)
    to_channel: str = Field(..., max_length=32)
    package_name: Optional[str] = None
    build_job_id: Optional[int] = None


class PromotionResponse(BaseModel):
    id: int
    release_id: int
    from_channel: str
    to_channel: Optional[str] = None
    status: str = "pending"
    package_id: Optional[int] = None
    package_name: Optional[str] = None
    build_job_id: Optional[int] = None
    promoted_by_id: Optional[int] = None
    created_at: datetime
    decided_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# --- Repository ---
class RepositoryResponse(BaseModel):
    id: int
    release_id: int
    channel: str
    architecture: str
    r2_prefix: str
    last_compose_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Schedule ---
class ScheduleCreate(BaseModel):
    release_id: int
    name: str = Field(..., max_length=128)
    kind: str = Field(..., max_length=32)
    cron_expression: Optional[str] = None
    config: Optional[str] = None
    is_enabled: bool = True


class ScheduleUpdate(BaseModel):
    cron_expression: Optional[str] = None
    config: Optional[str] = None
    is_enabled: Optional[bool] = None


class ScheduleResponse(BaseModel):
    id: int
    release_id: int
    name: str
    kind: str
    cron_expression: Optional[str] = None
    is_enabled: bool
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Audit ---
class AuditLogResponse(BaseModel):
    id: int
    account_id: Optional[int] = None
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    details: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Pagination ---
class PaginatedMeta(BaseModel):
    total: int
    page: int
    per_page: int
    pages: int


# --- Login ---
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
