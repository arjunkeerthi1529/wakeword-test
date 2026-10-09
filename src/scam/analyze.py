"""Stateless analysis of a piece of conversation text — the engine behind POST /analyze.

Runs the same pipeline as live monitoring (rules, trivial-line skip, model review,
refusal/safety-advice veto) on text you supply, with no microphone and no call state.
"""
import time
from typing import Dict, List

from . import rules
from .api_models import (Alert, AnalyzeRequest, AnalyzeResponse, AnalyzeSegment, LLMFinding,
                         RuleFinding)
from .llm_review import build_snapshot, is_trivial, validate
from .monitor import _DEFAULT_PRESSURE_MESSAGE, _LLM_MESSAGES, split_sentences
from .session import Segment, Session, rank


def analyze(req: AnalyzeRequest, llm, system_prompt: str, cfg) -> AnalyzeResponse:
    t0 = time.monotonic()
    lines: List[str] = list(req.segments) if req.segments else split_sentences(req.text or "")

    session = Session()
    for i, text in enumerate(req.context):
        session.segments.append(Segment(f"c{i + 1}", i * 3000, i * 3000 + 2500, text))
    first_new = len(session.segments)
    for i, text in enumerate(lines):
        n = first_new + i
        session.segments.append(Segment(f"s{i + 1}", n * 3000, n * 3000 + 2500, text))
    session.reviewed_upto = first_new
    new_segments = session.segments[first_new:]

    # 1. rules, per line (the previous line is passed so a request split across two lines is caught)
    segments_out: List[AnalyzeSegment] = []
    findings: Dict[str, rules.RuleResult] = {}
    for i, seg in enumerate(new_segments):
        prev = session.segments[first_new + i - 1].text if first_new + i > 0 else ""
        result = rules.evaluate(seg.text, seg.id, prev)
        findings[seg.id] = result
        segments_out.append(AnalyzeSegment(
            segment_id=seg.id, text=seg.text, skipped_by_llm=is_trivial(seg.text),
            rule=RuleFinding(level=result.level, label=result.label, evidence=result.evidence,
                             shadow=not cfg.rules_enabled),
        ))

    # 2. one model review of everything that is worth reviewing
    finding = LLMFinding(used=False)
    verdict = None
    if req.use_llm:
        snap = build_snapshot(session)
        if snap is None:
            finding.skipped_reason = "every line is too short to carry a request"
        else:
            t1 = time.monotonic()
            try:
                raw, info = llm.review(system_prompt, snap.prompt, cfg.llm_max_tokens, cfg.llm_timeout_s)
                finding.reply = raw.strip()
                finding.mode = info.get("mode")
                finding.prompt_tokens = info.get("prompt_n")
                finding.output_tokens = info.get("predicted_n")
                verdict = validate(raw, snap)
                if verdict is None:
                    finding.error = "model reply could not be parsed"
                else:
                    finding.used = True
                    finding.risk, finding.segment_id, finding.speaker = (
                        verdict.risk, verdict.segment_id, verdict.speaker)
                    finding.vetoed = (verdict.risk != "none" and cfg.llm_advice_veto
                                      and rules.looks_like_safety_advice(verdict.evidence))
            except Exception as exc:                      # network error, timeout, bad HTTP status
                finding.error = str(exc)
            finding.duration_s = round(time.monotonic() - t1, 2)

    # 3. combine into alerts: one per line, the higher risk wins, sources merge
    alerts: Dict[str, Alert] = {}
    if cfg.rules_enabled:
        for seg in new_segments:
            r = findings[seg.id]
            if r.level != "none":
                alerts[seg.id] = Alert(segment_id=seg.id, risk=r.level, speaker="unknown",
                                       evidence=r.evidence, message=r.message or _DEFAULT_PRESSURE_MESSAGE,
                                       source="rule", revision=1)
    if verdict is not None and verdict.risk != "none" and not finding.vetoed:
        existing = alerts.get(verdict.segment_id)
        if existing is None:
            alerts[verdict.segment_id] = Alert(
                segment_id=verdict.segment_id, risk=verdict.risk, speaker=verdict.speaker,
                evidence=verdict.evidence, message=_LLM_MESSAGES[verdict.risk], source="llm", revision=1)
        else:
            if rank(verdict.risk) > rank(existing.risk):
                existing.risk = verdict.risk
            if verdict.speaker != "unknown":
                existing.speaker = verdict.speaker
            existing.source = "rule+llm"

    ordered = [alerts[s.id] for s in new_segments if s.id in alerts]
    top = max((a.risk for a in ordered), key=rank, default="none")
    return AnalyzeResponse(
        risk=top,
        complete=not (req.use_llm and finding.error is not None),
        alerts=ordered, segments=segments_out, llm=finding,
        rules_enabled=cfg.rules_enabled, took_s=round(time.monotonic() - t0, 2),
    )
