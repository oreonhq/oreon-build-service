from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    items: List[T]
    total: int
    offset: int = 0


class RoleNameSchema(str, Enum):
    admin = "admin"
    maintainer = "maintainer"


class AccountCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8)
    role: RoleNameSchema


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


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
    default_distgit_branch: str = Field(
        default="main",
        max_length=128,
        description="Default Git branch in oreonhq/rpm-specfiles for distgit builds",
    )
    base_repos: List[ReleaseRepoCreate] = []


class ReleaseUpdate(BaseModel):
    description: Optional[str] = None
    architectures: Optional[str] = None
    default_channel: Optional[ReleaseChannelSchema] = None
    default_repo_branch: Optional[str] = Field(None, max_length=32)
    default_distgit_branch: Optional[str] = Field(None, max_length=128)
    default_mock_environment_id: Optional[int] = None
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
    default_distgit_branch: str
    default_mock_environment_id: Optional[int] = None
    is_active: bool
    created_at: datetime
    base_repos: List[ReleaseRepoResponse] = []

    model_config = ConfigDict(from_attributes=True)


_MOCK_REPO_MAX = 32


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
        v, allow_none=allow_none, per_field_cap=_MOCK_REPO_MAX
    )


class MockEnvironmentCreate(BaseModel):
    release_id: int
    name: str = Field(..., max_length=128)
    config_name: str = Field(..., max_length=128, description="Base config name; at build time worker uses <config_name>-<arch>")
    config_content: Optional[str] = None
    additional_repo_urls: List[str] = Field(default_factory=list, description="Extra mock --addrepo URLs")
    is_available: bool = True
    priority: int = 0

    @field_validator("additional_repo_urls", mode="before")
    @classmethod
    def _repos_create_additional(cls, v: object) -> List[str]:
        out = _validate_mock_additional_repo_urls(v, allow_none=False)
        return list(out) if out is not None else []


class MockEnvironmentUpdate(BaseModel):
    name: Optional[str] = None
    config_name: Optional[str] = None
    config_content: Optional[str] = None
    additional_repo_urls: Optional[List[str]] = None
    is_available: Optional[bool] = None
    priority: Optional[int] = None

    @field_validator("additional_repo_urls", mode="before")
    @classmethod
    def _repos_update_add(cls, v: object) -> Optional[List[str]]:
        if v is None:
            return None
        return _validate_mock_additional_repo_urls(v, allow_none=False)


class MockEnvironmentResponse(BaseModel):
    id: int
    release_id: int
    name: str
    config_name: str
    architecture: Optional[str] = None
    additional_repo_urls: List[str] = Field(default_factory=list)
    is_available: bool
    priority: int
    created_at: datetime
    config_content: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SourceTypeSchema(str, Enum):
    url = "url"
    upload = "upload"
    scm = "scm"
    distgit = "distgit"


class NewPackageForBuild(BaseModel):
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


class DeadPackageItem(BaseModel):
    id: int
    name: str
    sample_error: Optional[str] = None


class PurgeDeadPackagesResponse(BaseModel):
    deleted: int
    failed: List[str] = Field(default_factory=list)


class PurgeDeadPackagesAccepted(BaseModel):
    status: str = "accepted"
    message: str
    package_count: int


class PackageVersionResponse(BaseModel):
    id: int
    package_id: int
    version: str
    release_tag: Optional[str] = None
    git_sha: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BuildStatusSchema(str, Enum):
    pending = "pending"
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"
    skipped = "skipped"


class BuildTrigger(BaseModel):
    package_id: Optional[int] = Field(None, description="Existing package ID (omit if using new_package)")
    new_package: Optional[NewPackageForBuild] = Field(None, description="Create this package and build it")
    release_id: int = Field(..., description="Release to build for")
    side_tag_id: int = Field(..., description="Side tag to publish into (required)")
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
    display_status: str = ""
    signing_status: Optional[str] = None
    priority: int
    created_at: datetime
    completed_at: Optional[datetime] = None
    layer_plan_id: Optional[int] = None
    side_tag_id: Optional[int] = None
    side_tag_name: Optional[str] = None
    branch: Optional[str] = None
    layer_index: Optional[int] = None
    blocked_reason: Optional[str] = None
    rpmbuild_plan_id: Optional[int] = None
    rpmbuild_layer_index: Optional[int] = None
    scan_state: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class BuildTriggerAccepted(BaseModel):
    status: str = "accepted"
    message: str
    package_count: Optional[int] = None


class DistgitBatchPackageItem(BaseModel):
    name: str = Field(..., max_length=256)
    spec_path: Optional[str] = Field(None, max_length=512, description="Defaults to releasename/name/name.spec")


class DistgitBatchTrigger(BaseModel):
    release_id: int = Field(..., ge=1)
    side_tag_id: int = Field(..., description="Side tag to publish into (required)")
    packages: List[DistgitBatchPackageItem] = Field(..., min_length=1)
    architectures: List[str] = Field(default_factory=lambda: ["x86_64", "aarch64"])
    preferred_mock_environment_id: Optional[int] = None
    distgit_url: Optional[str] = Field(None, max_length=1024)
    distgit_branch: Optional[str] = Field(None, max_length=128)
    priority: int = 0
    skip_complete_arches: bool = Field(
        default=False,
        description="If true, skip arches that already have a successful build for this package and release.",
    )


class SideTagCreate(BaseModel):
    release_id: int = Field(..., ge=1)
    name: str = Field(..., min_length=1, max_length=128)


class SideTagInvite(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)


class SideTagPromoteRequest(BaseModel):
    to_branch: str = Field(..., description="dev, testing, or stable")


class SideTagDeletePackages(BaseModel):
    names: List[str] = Field(..., min_length=1)


class BranchPromoteRequest(BaseModel):
    release_id: int = Field(..., ge=1)
    from_branch: str = Field(..., min_length=1, max_length=128)
    to_branch: str = Field(..., min_length=1, max_length=128)


class SideTagMemberResponse(BaseModel):
    account_id: int
    username: str

    model_config = ConfigDict(from_attributes=True)


class SideTagResponse(BaseModel):
    id: int
    release_id: int
    releasename: Optional[str] = None
    name: str
    owner_account_id: Optional[int] = None
    owner_username: Optional[str] = None
    created_from: str
    status: str = "idle"
    created_at: datetime
    last_activity_at: datetime
    members: List[SideTagMemberResponse] = Field(default_factory=list)
    can_access: bool = False

    model_config = ConfigDict(from_attributes=True)


class SideTagPackageRow(BaseModel):
    package_id: int
    package_name: str
    side_tag_id: int
    side_tag_name: str
    release_id: int
    releasename: Optional[str] = None
    latest_job_status: Optional[str] = None
    latest_job_id: Optional[int] = None
    updated_at: Optional[datetime] = None


class LayerJobRow(BaseModel):
    job_id: int
    package_name: str
    status: str
    layer_index: Optional[int] = None
    blocked_reason: Optional[str] = None
    architecture: Optional[str] = None
    blocked_on: List[str] = Field(default_factory=list)


class RpmbuildPlanCreate(BaseModel):
    release_id: int = Field(..., ge=1)
    repo_branch: str = Field(default="dev", min_length=1, max_length=64)
    seed_packages: List[str] = Field(default_factory=list)
    architectures: List[str] = Field(default=["x86_64", "aarch64"])
    priority: int = 0
    distgit_url: Optional[str] = Field(None, max_length=1024)
    distgit_branch: Optional[str] = Field(None, max_length=128)


class ChainBuildTrigger(BaseModel):
    chain: str = Field(..., min_length=1, max_length=200000)
    release_id: int = Field(..., ge=1)
    side_tag_id: int = Field(..., ge=1)
    architectures: List[str] = Field(default=["x86_64", "aarch64"])
    priority: int = 0
    preferred_mock_environment_id: Optional[int] = None
    distgit_url: Optional[str] = Field(None, max_length=1024)
    distgit_branch: Optional[str] = Field(None, max_length=128)
    maintenance: bool = False


class DiscoveryBuildTrigger(BaseModel):
    packages: List[str] = Field(..., min_length=1)
    release_id: int = Field(..., ge=1)
    side_tag_id: int = Field(..., ge=1)
    architectures: List[str] = Field(default=["x86_64", "aarch64"])
    priority: int = 0
    preferred_mock_environment_id: Optional[int] = None
    distgit_url: Optional[str] = Field(None, max_length=1024)
    distgit_branch: Optional[str] = Field(None, max_length=128)


class RpmbuildPlanResponse(BaseModel):
    id: int
    release_id: int
    releasename: str
    repo_branch: str
    distgit_url: str
    distgit_branch: str
    seed_mode: str
    seed_packages: List[str]
    status: str
    current_open_layer: int
    total_layers: int
    queued_jobs: int
    created_at: datetime
    updated_at: datetime


class ChainPlanStatusResponse(BaseModel):
    chain_plan_id: int
    status: str
    groups: List[List[str]] = Field(default_factory=list)
    current_group: int
    total_groups: int
    jobs: List[LayerJobRow] = Field(default_factory=list)
    counts: dict = Field(default_factory=dict)
    error_message: Optional[str] = None
    releasename: Optional[str] = None
    side_tag_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    threats: List["ChainThreatRow"] = Field(default_factory=list)


class ChainThreatRow(BaseModel):
    finding_id: int
    job_id: int
    package_name: Optional[str] = None
    engine: str
    rule: str
    file_path: str


class CurrentFailureItem(BaseModel):

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
    layer_plan_id: Optional[int] = None
    last_attempt_id: Optional[int] = None
    last_error_message: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class BuildAttemptResponse(BaseModel):
    id: int
    build_job_id: int
    worker_id: Optional[int] = None
    attempt_number: int
    status: str
    display_status: str = ""
    mock_succeeded_at: Optional[datetime] = None
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
    is_signed: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


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
    is_burst_worker: bool = False
    burst_whitelist: List[str] = []
    gcp_instance_name: Optional[str] = Field(None, max_length=253)
    gcp_zone: Optional[str] = Field(None, max_length=128)


class WorkerUpdate(BaseModel):
    architecture: Optional[str] = None
    is_burst_worker: Optional[bool] = None
    burst_whitelist: Optional[List[str]] = None
    gcp_instance_name: Optional[str] = Field(None, max_length=253)
    gcp_zone: Optional[str] = Field(None, max_length=128)


class WorkerResponse(BaseModel):
    id: int
    name: str
    state: str
    architecture: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    enrolled_at: datetime
    is_burst_worker: bool = False
    burst_whitelist: List[str] = []
    gcp_instance_name: Optional[str] = None
    gcp_zone: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


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


class BulkPromoteRequest(BaseModel):
    promotion_ids: Optional[List[int]] = None
    all_pending: bool = False


class BulkPromoteAccepted(BaseModel):
    status: str = "accepted"
    message: str
    promotion_count: int


class RepositoryResponse(BaseModel):
    id: int
    release_id: int
    channel: str
    architecture: str
    r2_prefix: str
    last_compose_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


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


class AuditLogResponse(BaseModel):
    id: int
    account_id: Optional[int] = None
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    details: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PaginatedMeta(BaseModel):
    total: int
    page: int
    per_page: int
    pages: int


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
