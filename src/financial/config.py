import os
from dataclasses import dataclass


@dataclass
class FinancialConfig:
    llm_base_url: str   # shared with the other services' LLM_BASE_URL
    llm_model: str       # shared with LLM_MODEL (see src/work/config.py's note on Ollama vs llama.cpp)
    db_path: str
    port: int
    timezone: str


def get_financial_config() -> "FinancialConfig":
    return FinancialConfig(
        llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:8080"),
        llm_model=os.getenv("LLM_MODEL", "local"),
        db_path=os.getenv("FINANCIAL_DB_PATH", "data/financial.db"),
        port=int(os.getenv("FINANCIAL_PORT", "8002")),
        timezone=os.getenv("FINANCIAL_TIMEZONE", "Asia/Kolkata"),
    )
