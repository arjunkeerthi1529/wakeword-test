# src/financial — expense tracker + statement import, as a standalone service.
# Ported from a separate reference project's financial profile (exact paise
# arithmetic, locked categories, CSV/PDF import, LLM-interpreted questions)
# and flattened to match this repo's src/work service style: its own
# config.py/db.py/server.py, direct LLM calls (no shared scheduler), blocking
# endpoints instead of a job/polling layer. Run with: python -m src.financial
