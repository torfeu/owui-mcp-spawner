from pydantic import BaseModel, Field, field_validator
from typing import Optional, Any
from enum import Enum


class MCPStatus(str, Enum):
    installing = "installing"
    installed = "installed"
    starting = "starting"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    failed = "failed"
    dependency_error = "dependency_error"


class IdentityMode(str, Enum):
    """How much this instance cares who the end user is.

    off       — as before: only the MCP Bearer token is checked (default).
    optional  — a valid user token is published to the tools, a missing one is
                not an error, and no access rules are applied. For bringing an
                instance over and for diagnosis; not a boundary, because
                "no token" still gets in.
    required  — no verified user, no tool call. Access rules from app/policy.py
                apply on top, and they deny by default.
    """

    off = "off"
    optional = "optional"
    required = "required"


class MachineIdentity(BaseModel):
    """A caller that has no OpenWebUI login — an agent CLI, a script, a cron job.

    Assigned, not verified: whoever reaches this instance's port with the MCP
    Bearer token *is* this identity. That is no weaker than the instance was
    before (the token was already the only gate), and it buys something real —
    the access rules apply to machines too, so an agent can be given three
    tools instead of all of them.

    Give it its own id and its own account. Pointing it at a person's account
    hands that person's data to anyone holding the token.
    """

    sub: str = ""
    name: str = ""
    role: str = ""

    def as_identity(self):
        from .identity import Identity, SOURCE_MACHINE
        return Identity(sub=self.sub, name=self.name, role=self.role, source=SOURCE_MACHINE)


class ContentConfig(BaseModel):
    """Where this instance may put the files it produces — off by default.

    Opt-in per instance (decision 4): most tools never write a file, and an
    instance that does not ask for storage should not get a folder, a valve
    filled behind its back, or its results rewritten.

    *url_prefix* is what the tool's own links start with. Tools written for
    OpenWebUI return `/cache/files/...`, which resolves against OpenWebUI in
    the browser and finds nothing of ours — the runner rewrites that prefix to
    our download URL on the way out.
    """

    enabled: bool = False
    url_prefix: str = "/cache/files/"

    @field_validator("url_prefix")
    @classmethod
    def _ensure_trailing_slash(cls, v: str) -> str:
        # Without the trailing slash the rewrite pattern captures "/name.docx"
        # as the filename, which the path check refuses — every link would then
        # silently stay unrewritten. Normalised here so a hand-edited config is
        # covered the same as the API and the UI.
        v = str(v).strip()
        if v and not v.endswith("/"):
            v += "/"
        return v


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(ge=1024, le=65535)
    endpoint: str = "/mcp"

    @field_validator("host")
    @classmethod
    def host_must_be_local(cls, v: str) -> str:
        allowed = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
        if v not in allowed:
            raise ValueError(f"Host must be local or 0.0.0.0. Got: {v}")
        return v


class InstallConfig(BaseModel):
    dependencies: list[str] = []
    upgrade: bool = False


class ToolSourceConfig(BaseModel):
    type: str = "openwebui_json"
    path: str


class LifecycleConfig(BaseModel):
    auto_start: bool = False
    restart_on_change: bool = True


class MCPConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    # UI-only grouping label; never part of OpenWebUI tool JSON import/export
    category: str = ""
    locked: bool = False
    server: ServerConfig
    install: InstallConfig = InstallConfig()
    tool_source: ToolSourceConfig
    values: dict[str, Any] = {}
    lifecycle: LifecycleConfig = LifecycleConfig()
    venv: str = "default"
    identity_mode: IdentityMode = IdentityMode.off
    # Stands in when no user token arrives — for callers that have no login.
    machine_identity: Optional[MachineIdentity] = None
    content: ContentConfig = ContentConfig()

    @field_validator("id")
    @classmethod
    def id_safe(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_\-]+", v):
            raise ValueError(
                "ID must contain only letters, digits, underscores and hyphens"
            )
        return v


class MCPInstance(BaseModel):
    id: str
    name: str
    description: str = ""
    category: str = ""
    status: MCPStatus = MCPStatus.stopped
    port: int
    host: str
    endpoint: str
    pid: Optional[int] = None
    url: str = ""
    error: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.url:
            self.url = f"http://{self.host}:{self.port}{self.endpoint}"
