"""Homebound configuration — YAML-driven with sensible defaults.

Loads homebound.yaml, merges defaults, and provides the HomeboundConfig
dataclass that all other modules depend on.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("homebound.config")


def _build_dataclass(cls, raw: dict):
    """Build a dataclass from a dict, ignoring unknown keys.

    Only passes keys that correspond to actual dataclass fields,
    letting field defaults handle anything missing from the YAML.
    This ensures defaults are defined in exactly one place.
    """
    known_fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known_fields})


@dataclass
class OrchestratorConfig:
    """Core orchestrator identity and naming."""

    name: str = "homebound"
    aliases: list[str] = field(default_factory=list)


@dataclass
class TransportConfig:
    """Transport layer configuration."""

    type: str = "slack"
    channel_id: str = ""
    token_env: str = "SLACK_BOT_TOKEN"
    lookback_minutes: int = 5
    message_format: str = "*[{name}]* {message}"
    strip_prefixes: list[str] = field(default_factory=lambda: ["claude-desktop"])
    ignored_prefixes: list[str] = field(default_factory=list)
    post_command_template: str = "echo '[{session_name}] {message}'"
    http_timeout: int = 10
    poll_limit: int = 20


@dataclass
class TrackerConfig:
    """Issue tracker configuration."""

    type: str = "github"
    project_dir: str = "."
    admin_pattern: str = r"^@{name}\s+(.+)"
    item_query_pattern: str = r"(\d+)\s*\?$"
    command_timeout: int = 30


@dataclass
class RuntimeConfig:
    """Agent runtime configuration."""

    type: str = "claude-code"
    command: str = "claude"
    idle_markers: list[str] = field(default_factory=lambda: ["\u276f", "> "])
    exit_command: str = "/exit"
    env_unset: list[str] = field(default_factory=lambda: ["CLAUDECODE"])


@dataclass
class SessionsConfig:
    """Child session lifecycle configuration."""

    max_concurrent: int = 5
    idle_timeout: int = 1800  # 30 min
    init_timeout: int = 60
    poll_interval: int = 10
    max_message_len: int = 4000
    idle_warning_threshold: int = 3
    close_grace_period: float = 2.0
    max_retries: int = 3
    outage_threshold: int = 3       # consecutive poll failures before escalating
    outage_max_interval: int = 120  # max seconds between polls during outage
    error_patterns: list[str] = field(default_factory=lambda: [
        r"(?i)api\s*error.*5\d{2}",
        r"(?i)internal\s+server\s+error",
        r"(?i)api_error",
        r"(?i)overloaded_error",
        r"(?i)rate\s*limit\s*(exceeded|error)",
        r"(?i)connection\s*(refused|reset|timed?\s*out)",
    ])
    error_scan_lines: int = 20

    def __post_init__(self):
        # Validate error_patterns compile as regex
        for idx, pattern in enumerate(self.error_patterns):
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(
                    f"SessionsConfig.error_patterns[{idx}] is not a valid regex: {e}"
                ) from e



@dataclass
class PromptRelayConfig:
    """Runtime prompt relay configuration."""

    enabled: bool = True
    scan_lines: int = 30
    poll_every_cycles: int = 1
    option_patterns: list[str] = field(
        default_factory=lambda: [
            r"^\s*\d+[\)\.\:]\s*(.+)$",
            r"^\s*[A-Za-z][\)\.]\s*(.+)$",
            r"^\s*-\s\[[ xX]\]\s*(.+)$",
        ]
    )
    question_mark_required: bool = True
    ttl_seconds: int = 900
    max_pending_per_issue: int = 1

    def __post_init__(self):
        for field_name in ("enabled", "question_mark_required"):
            value = getattr(self, field_name)
            if not isinstance(value, bool):
                raise TypeError(
                    f"PromptRelayConfig.{field_name} must be a bool, not {type(value).__name__}."
                )
        for field_name in ("scan_lines", "poll_every_cycles", "ttl_seconds", "max_pending_per_issue"):
            value = getattr(self, field_name)
            if not isinstance(value, int):
                raise TypeError(
                    f"PromptRelayConfig.{field_name} must be an int, not {type(value).__name__}."
                )
            if value <= 0:
                raise ValueError(f"PromptRelayConfig.{field_name} must be > 0.")
        if not isinstance(self.option_patterns, list):
            raise TypeError(
                f"PromptRelayConfig.option_patterns must be a list, not {type(self.option_patterns).__name__}."
            )
        normalized_patterns: list[str] = []
        for idx, pattern in enumerate(self.option_patterns):
            if not isinstance(pattern, str):
                raise TypeError(
                    f"PromptRelayConfig.option_patterns[{idx}] must be a string, "
                    f"not {type(pattern).__name__}."
                )
            cleaned = pattern.strip()
            if not cleaned:
                raise ValueError(f"PromptRelayConfig.option_patterns[{idx}] cannot be empty.")
            try:
                re.compile(cleaned)
            except re.error as e:
                raise ValueError(
                    f"PromptRelayConfig.option_patterns[{idx}] is not a valid regex: {e}"
                ) from e
            normalized_patterns.append(cleaned)
        self.option_patterns = normalized_patterns


@dataclass
class RoutingConfig:
    """Smart routing configuration."""

    thread_routing: bool = True
    keyword_routing: bool = True
    llm_routing: bool = False
    llm_model: str = "claude-haiku-4-5"
    keyword_match_threshold: int = 1
    auto_spawn_on_no_match: bool = True
    max_message_map_size: int = 200
    enrich_interval_cycles: int = 6  # Refresh keywords every N poll cycles (~60s at 10s poll)
    thread_poll_max_age: int = 1800  # Only poll threads < 30 min old
    thread_poll_max_threads: int = 10  # Max threads polled per cycle
    busy_recency_seconds: int = 30  # Agent is "busy" if messaged within this window
    busy_check_tmux: bool = False  # Also check tmux idle markers (slower, more accurate)


_SLACK_FORMATTING_RULES = (
    "SLACK FORMATTING (mandatory): "
    "Format all Slack messages using Slack mrkdwn syntax for readability: "
    "use *bold* for headings and key terms, _italic_ for emphasis, "
    "`code` for values/commands, ```code blocks``` for multi-line output, "
    "bullet lists with • or -, and > for blockquotes. "
    "Structure responses with clear sections and line breaks. "
    "Never post walls of unformatted text. "
)


@dataclass
class ModeConfig:
    """Configuration for a single child mode (task, freeform, chat)."""

    keyword: str = ""
    prompt_template: str = ""


@dataclass
class SecurityConfig:
    """Security configuration."""

    allowed_users: list[str] = field(default_factory=list)
    destructive_confirm_timeout: int = 60
    allow_open_channel: bool = False
    allow_bots: bool = False
    allow_admin_takeover: bool = False

    def __post_init__(self):
        if isinstance(self.allowed_users, str):
            raise TypeError(
                "SecurityConfig.allowed_users must be a list, not a str. "
                "Use YAML list syntax: [\"U123\"]"
            )
        if not isinstance(self.allowed_users, list):
            raise TypeError(
                f"SecurityConfig.allowed_users must be a list, not {type(self.allowed_users).__name__}."
            )
        normalized_users: list[str] = []
        for idx, user_id in enumerate(self.allowed_users):
            if not isinstance(user_id, str):
                raise TypeError(
                    f"SecurityConfig.allowed_users[{idx}] must be a string, "
                    f"not {type(user_id).__name__}."
                )
            cleaned = user_id.strip()
            if not cleaned:
                raise ValueError(
                    f"SecurityConfig.allowed_users[{idx}] cannot be empty."
                )
            normalized_users.append(cleaned)
        self.allowed_users = normalized_users
        for field_name in ("allow_open_channel", "allow_bots", "allow_admin_takeover"):
            value = getattr(self, field_name)
            if not isinstance(value, bool):
                raise TypeError(
                    f"SecurityConfig.{field_name} must be a bool, not {type(value).__name__}. "
                    "Use YAML booleans: true/false (without quotes)."
                )


@dataclass
class HomeboundConfig:
    """Top-level configuration for a Homebound instance."""

    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    prompt_relay: PromptRelayConfig = field(default_factory=PromptRelayConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    modes: dict[str, ModeConfig] = field(default_factory=dict)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    close_commands: set[str] = field(
        default_factory=lambda: {"close", "stop", "done", "exit", "quit", "kill"}
    )
    default_mode: str = "task"
    # Named runtime pools: maps pool name → RuntimeConfig.
    # Each pool gets its own label prefix (e.g., "Claude1", "Codex2").
    runtimes: dict[str, RuntimeConfig] = field(
        default_factory=lambda: {"agent": RuntimeConfig()}
    )

    # Derived properties
    @property
    def name(self) -> str:
        return self.orchestrator.name

    @property
    def tmux_session_name(self) -> str:
        return self.orchestrator.name

    @property
    def project_dir(self) -> Path:
        return Path(self.tracker.project_dir).resolve()

    @property
    def pool_names(self) -> list[str]:
        """Sorted list of configured pool names."""
        return sorted(self.runtimes.keys())

    @property
    def default_pool(self) -> str:
        """The default pool name (first alphabetically)."""
        return sorted(self.runtimes.keys())[0]

    @property
    def ignored_prefixes(self) -> list[str]:
        """All prefixes to ignore when polling (prevents re-routing loops)."""
        base = [self.name]
        base.extend(self.orchestrator.aliases)
        base.extend(self.transport.ignored_prefixes)
        for pool in self.pool_names:
            label = pool.capitalize()
            base.extend([label, label.lower(), f"{label.lower()}-"])
        return base

    @property
    def admin_pattern(self) -> str:
        """Admin command pattern with {name} substituted, including aliases."""
        names = [re.escape(self.name)] + [re.escape(a) for a in self.orchestrator.aliases]
        name_group = "|".join(names)
        return self.tracker.admin_pattern.format(name=f"(?:{name_group})")

    def pool_label(self, pool_name: str) -> str:
        """User-facing label prefix for a pool (e.g., 'claude' → 'Claude')."""
        return pool_name.capitalize()

    def pool_window_prefix(self, pool_name: str) -> str:
        """Tmux window prefix for a pool (e.g., 'claude' → 'CLAUDE-')."""
        return f"{self.pool_label(pool_name).upper()}-"

    def pool_session_prefix(self, pool_name: str) -> str:
        """Transport session prefix for a pool (e.g., 'claude' → 'claude-')."""
        return f"{pool_name.lower()}-"

    def __post_init__(self):
        # Adapter instance caches (not dataclass fields)
        self._runtime_instances: dict[str, object] = {}
        self._transport_instance = None
        self._tracker_instance = None

        # Merge user-provided modes with built-in defaults.
        # User-provided modes override defaults; missing modes get defaults.
        default_modes = {
            "task": ModeConfig(
                prompt_template="Work on item {work_item_label}. Read the full details and complete the task.",
            ),
            "freeform": ModeConfig(
                keyword="freeform:",
                prompt_template=(
                    "You are working on {work_item_label}. "
                    "Read the issue: gh issue view {item_id} --comments . "
                    "Your task instructions are delimited below. "
                    "--- BEGIN TASK --- {task_text} --- END TASK --- "
                    "COMMUNICATION RULES (mandatory): "
                    "1. The user CANNOT see your tmux output. Slack is your ONLY channel. "
                    "2. Whenever you need input, a decision, or clarification — post the "
                    "question to Slack and then wait. Do NOT just sit at the prompt. "
                    "3. Post progress updates to Slack as you complete each step. "
                    "4. Post detailed results to the GitHub issue when done. "
                    + _SLACK_FORMATTING_RULES +
                    "Slack command: {post_command} . "
                    "GitHub command: gh issue comment {item_id} --body 'your results' ."
                ),
            ),
            "chat": ModeConfig(
                keyword="chat:",
                prompt_template=(
                    "You are handling {work_item_label}. "
                    "--- BEGIN TASK --- {task_text} --- END TASK --- "
                    + _SLACK_FORMATTING_RULES +
                    "Post your results to Slack when done: {post_command}"
                ),
            ),
        }
        for key, val in default_modes.items():
            self.modes.setdefault(key, val)

    def get_runtime(self, pool_name: str | None = None):
        """Create or return cached runtime instance.

        Args:
            pool_name: Pool name to look up. If None or empty, uses default_pool.
        """
        return self.get_runtime_for_pool(pool_name or self.default_pool)

    def get_runtime_for_pool(self, pool_name: str):
        """Create or return cached runtime instance for a named pool.

        Raises ValueError if pool_name is not in self.runtimes.
        """
        if pool_name in self._runtime_instances:
            return self._runtime_instances[pool_name]

        runtime_config = self.runtimes.get(pool_name)
        if runtime_config is None:
            raise ValueError(
                f"Unknown runtime pool: {pool_name!r}. "
                f"Available: {', '.join(self.runtimes.keys())}"
            )

        from homebound.runtimes import RUNTIME_REGISTRY

        runtime_cls = RUNTIME_REGISTRY.get(runtime_config.type)
        if runtime_cls is None:
            raise ValueError(
                f"Unknown runtime type for pool {pool_name!r}: {runtime_config.type}. "
                f"Available: {', '.join(RUNTIME_REGISTRY.keys())}"
            )

        instance = runtime_cls.from_config(runtime_config)
        self._runtime_instances[pool_name] = instance
        return instance

    def get_transport(self):
        """Create or return cached transport instance from configuration."""
        if self._transport_instance is not None:
            return self._transport_instance

        from homebound.transports import TRANSPORT_REGISTRY

        transport_cls = TRANSPORT_REGISTRY.get(self.transport.type)
        if transport_cls is None:
            raise ValueError(
                f"Unknown transport type: {self.transport.type}. "
                f"Available: {', '.join(TRANSPORT_REGISTRY.keys())}"
            )

        self._transport_instance = transport_cls.from_config(
            self.transport, agent_name=self.name,
        )
        return self._transport_instance

    def get_tracker(self):
        """Create or return cached tracker instance from configuration."""
        if self._tracker_instance is not None:
            return self._tracker_instance

        from homebound.trackers import TRACKER_REGISTRY

        tracker_cls = TRACKER_REGISTRY.get(self.tracker.type)
        if tracker_cls is None:
            raise ValueError(
                f"Unknown tracker type: {self.tracker.type}. "
                f"Available: {', '.join(TRACKER_REGISTRY.keys())}"
            )

        self._tracker_instance = tracker_cls.from_config(self.tracker)
        return self._tracker_instance


def load_config(config_path: str | Path | None = None) -> HomeboundConfig:
    """Load configuration from a YAML file.

    Args:
        config_path: Path to homebound.yaml. If None, searches for
            homebound.yaml in the current directory.

    Returns:
        HomeboundConfig instance.
    """
    if config_path is None:
        config_path = Path.cwd() / "homebound.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        logger.info("No config file found at %s, using defaults", config_path)
        return HomeboundConfig()

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    return _parse_config(raw)


def _parse_config(raw: dict) -> HomeboundConfig:
    """Parse a raw YAML dict into a HomeboundConfig.

    Sub-dataclasses are built via _build_dataclass, which only passes
    YAML-present keys and lets dataclass field defaults handle the rest.
    This means each default is defined in exactly one place.
    """
    orchestrator = _build_dataclass(OrchestratorConfig, raw.get("orchestrator", {}))
    transport = _build_dataclass(TransportConfig, raw.get("transport", {}))
    tracker = _build_dataclass(TrackerConfig, raw.get("tracker", {}))
    sessions = _build_dataclass(SessionsConfig, raw.get("sessions", {}))
    prompt_relay = _build_dataclass(PromptRelayConfig, raw.get("prompt_relay", {}))
    routing = _build_dataclass(RoutingConfig, raw.get("routing", {}))
    security = _build_dataclass(SecurityConfig, raw.get("security", {}))

    # Migration aid: reject deprecated runtime: (singular) key
    if "runtime" in raw:
        raise ValueError(
            "The 'runtime:' (singular) config key is no longer supported. "
            "Please migrate to 'runtimes:' (plural). Example:\n"
            "  runtimes:\n"
            "    claude:\n"
            "      type: claude-code\n"
            "      command: claude"
        )

    # Parse named runtimes (multi-model pools).
    runtimes: dict[str, RuntimeConfig] = {}
    runtimes_raw = raw.get("runtimes", {})
    if runtimes_raw and isinstance(runtimes_raw, dict):
        for pool_name, pool_data in runtimes_raw.items():
            if not isinstance(pool_data, dict):
                continue
            if not pool_name.isalpha():
                raise ValueError(
                    f"Runtime pool name must be alphabetic, got: {pool_name!r}"
                )
            runtimes[pool_name.lower()] = _build_dataclass(RuntimeConfig, pool_data)

    # Parse modes (dict of ModeConfig, not a flat dataclass)
    modes_raw = raw.get("modes", {})
    default_mode = modes_raw.get("default", "task")
    modes = {}
    for mode_name, mode_data in modes_raw.items():
        if mode_name == "default":
            continue
        if isinstance(mode_data, dict):
            modes[mode_name] = _build_dataclass(ModeConfig, mode_data)

    # Build top-level config
    kwargs: dict = dict(
        orchestrator=orchestrator,
        transport=transport,
        tracker=tracker,
        sessions=sessions,
        prompt_relay=prompt_relay,
        routing=routing,
        modes=modes if modes else {},  # Let __post_init__ fill defaults if empty
        security=security,
        default_mode=default_mode,
        runtimes=runtimes if runtimes else {"agent": RuntimeConfig()},
    )

    if "close_commands" in raw:
        kwargs["close_commands"] = set(raw["close_commands"])

    return HomeboundConfig(**kwargs)
