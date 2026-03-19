"""Pydantic request/response schemas for API."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    base_repos: List[ReleaseRepoCreate] = []


class ReleaseUpdate(BaseModel):
    description: Optional[str] = None
    architectures: Optional[str] = None
    default_channel: Optional[ReleaseChannelSchema] = None
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
    is_active: bool
    created_at: datetime
    base_repos: List[ReleaseRepoResponse] = []

    model_config = ConfigDict(from_attributes=True)


# --- Mock environment ---
class MockEnvironmentCreate(BaseModel):
    release_id: int
    name: str = Field(..., max_length=128)
    config_name: str = Field(..., max_length=128, description="Base config name; at build time worker uses <config_name>-<arch>")
    config_content: Optional[str] = None
    is_available: bool = True
    priority: int = 0


class MockEnvironmentUpdate(BaseModel):
    name: Optional[str] = None
    config_name: Optional[str] = None
    config_content: Optional[str] = None
    is_available: Optional[bool] = None
    priority: Optional[int] = None


class MockEnvironmentResponse(BaseModel):
    id: int
    release_id: int
    name: str
    config_name: str
    architecture: Optional[str] = None
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
    gitlab_project_id: Optional[int] = None
    gitlab_web_url: Optional[str] = None


class PackageUpdate(BaseModel):
    description: Optional[str] = None
    preferred_mock_environment_id: Optional[int] = None
    gitlab_project_id: Optional[int] = None
    gitlab_web_url: Optional[str] = None


class PackageResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    preferred_mock_environment_id: Optional[int] = None
    gitlab_project_id: Optional[int] = None
    gitlab_web_url: Optional[str] = None
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

    @model_validator(mode="after")
    def require_package_or_new(self):
        if (self.package_id is None) == (self.new_package is None):
            raise ValueError("Provide exactly one of package_id or new_package")
        return self


class BuildJobResponse(BaseModel):
    id: int
    package_id: int
    release_id: int
    target_id: Optional[int] = None
    architecture: Optional[str] = None
    package_version_id: Optional[int] = None
    status: str
    priority: int
    created_at: datetime
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class BuildAttemptResponse(BaseModel):
    id: int
    build_job_id: int
    worker_id: Optional[int] = None
    attempt_number: int
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    log_r2_key: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime

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
