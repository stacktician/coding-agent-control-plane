"""Runtime configuration, read from the environment and `.env`."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    github_fork: str
    github_base_branch: str = "master"
    github_pat: SecretStr

    langfuse_otlp_endpoint: str = "https://cloud.langfuse.com/api/public/otel/v1/traces"
    langfuse_public_key: str
    langfuse_secret_key: SecretStr

    keycloak_realm_url: str = "http://localhost:8080/realms/control-plane"
    keycloak_client_id: str = "agent-worker"
    kc_agent_worker_secret: SecretStr

    gateway_mcp_url: str = "http://localhost:3000/mcp"
    temporal_address: str = "localhost:7233"
    temporal_task_queue: str = "coding-agent"
    jaeger_otlp_endpoint: str = "http://localhost:4318/v1/traces"
    poll_interval_s: int = 60

    agent_model: str = "claude-sonnet-5"
    agent_max_turns: int = 80

    target_install_command: str = 'pip install -e . "pytest>=8"'
    target_test_command: str = "python -m pytest -q --doctest-modules boltons tests"

    @property
    def keycloak_token_endpoint(self) -> str:
        return f"{self.keycloak_realm_url}/protocol/openid-connect/token"

    @property
    def fork_clone_url(self) -> str:
        return f"https://github.com/{self.github_fork}.git"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields come from the environment
