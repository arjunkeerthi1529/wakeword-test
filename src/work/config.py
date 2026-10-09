import os
from dataclasses import dataclass


@dataclass
class AgentConfig:
    # Email digest
    email_fetch_time: str    # "HH:MM" 24-hour, daily trigger
    agent_read_digest: bool  # speak the digest aloud via TTS after fetching
    llm_model: str           # "model" field sent to the chat-completions endpoint

    # Outbound drafts (reply / notify)
    agent_notify_email: str  # fixed address 'notify' drafts are sent to -- never LLM-chosen
    agent_send_mode: str     # "mock" (log + mark sent, no real email) or "smtp" (not implemented yet)

    # Database
    agent_db_path: str       # SQLite file path, created on first run

    # Inter-service communication
    agent_service_url: str   # URL the voice assistant uses to reach this service
    agent_port: int          # port this service listens on


def get_agent_config() -> "AgentConfig":
    return AgentConfig(
        email_fetch_time=os.getenv("EMAIL_FETCH_TIME", "08:00"),
        agent_read_digest=os.getenv("AGENT_READ_DIGEST", "false").lower() == "true",
        # llama.cpp serves a single loaded model and ignores this field, so
        # "local" was fine there -- Ollama's OpenAI-compatible endpoint
        # actually looks this up and 404s on anything not pulled, so it must
        # be overridable (e.g. LLM_MODEL=llama3.2:3b).
        llm_model=os.getenv("LLM_MODEL", "local"),
        agent_notify_email=os.getenv("AGENT_NOTIFY_EMAIL", "me@example.com"),
        agent_send_mode=os.getenv("AGENT_SEND_MODE", "mock"),
        agent_db_path=os.getenv("AGENT_DB_PATH", "data/agent.db"),
        agent_service_url=os.getenv("AGENT_SERVICE_URL", "http://localhost:8001"),
        agent_port=int(os.getenv("AGENT_PORT", "8001")),
    )
