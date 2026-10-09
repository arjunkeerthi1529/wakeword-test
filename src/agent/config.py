import os
from dataclasses import dataclass


@dataclass
class AgentConfig:
    # Email digest
    email_fetch_time: str    # "HH:MM" 24-hour, daily trigger
    agent_read_digest: bool  # speak the digest aloud via TTS after fetching

    # Database
    agent_db_path: str       # SQLite file path, created on first run


def get_agent_config() -> "AgentConfig":
    return AgentConfig(
        email_fetch_time=os.getenv("EMAIL_FETCH_TIME", "08:00"),
        agent_read_digest=os.getenv("AGENT_READ_DIGEST", "false").lower() == "true",
        agent_db_path=os.getenv("AGENT_DB_PATH", "data/agent.db"),
    )
