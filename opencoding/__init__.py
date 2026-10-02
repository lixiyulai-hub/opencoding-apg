"""OpenCoding W2 中文入口与本地服务。"""

from .service import (
    apply_approved,
    approve_preview,
    create_session,
    derive_frontier,
    list_sessions,
    preview_session,
    rollback,
    session_view,
    submit_answer,
)
from .product_loop import (
    read_product_run,
    resume_product_run,
    rollback_product_run,
    start_product_run,
)
from .codex_host import CodexHostError, CodexSkillHost, install_skill
from .agent_adapter import LocalAgentAdapter
from .host_connector import (
    ConnectorContract,
    FixtureConnector,
    HostConnectorError,
    OfflineConnectorRegistry,
    OfflineHostContract,
)
from .w5_acceptance import (
    SyntheticAcceptanceMatrix,
    SyntheticBeginnerScenario,
    W5AcceptanceError,
    build_synthetic_acceptance_matrix,
)
from .agent_tasks import LocalAgentTaskExecutor, preview_agent_tasks
from .target_adapters import (
    MacOSTargetAdapter,
    TargetAdapterError,
    WebTargetAdapter,
    WindowsTargetAdapter,
    get_target_adapter,
    target_adapter_status,
)

__all__ = [
    "apply_approved",
    "approve_preview",
    "create_session",
    "derive_frontier",
    "list_sessions",
    "preview_session",
    "rollback",
    "session_view",
    "submit_answer",
    "read_product_run",
    "resume_product_run",
    "rollback_product_run",
    "start_product_run",
    "CodexHostError",
    "CodexSkillHost",
    "install_skill",
    "LocalAgentAdapter",
    "ConnectorContract",
    "FixtureConnector",
    "HostConnectorError",
    "OfflineConnectorRegistry",
    "OfflineHostContract",
    "SyntheticAcceptanceMatrix",
    "SyntheticBeginnerScenario",
    "W5AcceptanceError",
    "build_synthetic_acceptance_matrix",
    "LocalAgentTaskExecutor",
    "preview_agent_tasks",
    "TargetAdapterError",
    "WindowsTargetAdapter",
    "MacOSTargetAdapter",
    "WebTargetAdapter",
    "get_target_adapter",
    "target_adapter_status",
]
