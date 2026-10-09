"""Summarise a scam-guard analysis log.

    python scripts/scam_log_report.py                       # latest call in logs/scam_events.jsonl
    python scripts/scam_log_report.py --call c3ca956        # a specific call
    python scripts/scam_log_report.py --all                 # every call
    python scripts/scam_log_report.py path/to/other.jsonl

Shows, per call: what was heard and how the rules and the LLM judged each line,
how long STT and the LLM took, and how long each alert took to appear
(measured from when the audio chunk was cut, about 0.6 s after speech ends).
"""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parents[1] / "logs" / "scam_events.jsonl"


def load(path):
    calls = defaultdict(list)
    order = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            cid = rec.get("call_id", "")
            if cid not in calls:
                order.append(cid)
            calls[cid].append(rec)
    return calls, order


def stats(values, unit="s"):
    if not values:
        return "n/a"
    values = sorted(values)
    p = lambda q: values[min(len(values) - 1, int(q * len(values)))]
    return (f"n={len(values)}  min {values[0]:.1f}{unit}  median {statistics.median(values):.1f}{unit}  "
            f"p90 {p(0.9):.1f}{unit}  max {values[-1]:.1f}{unit}")


def report(cid, recs):
    start = next((r for r in recs if r["kind"] == "call_start"), {})
    end = next((r for r in recs if r["kind"] == "call_end"), {})
    stt = [r for r in recs if r["kind"] == "stt"]
    llm = [r for r in recs if r["kind"] == "llm"]
    alerts = [r for r in recs if r["kind"] == "alert"]
    segs = [r for r in recs if r["kind"] == "segment"]
    rules = {r["segment_id"]: r for r in recs if r["kind"] == "rule"}
    errors = [r for r in recs if r["kind"] in ("error", "stt_error")]

    if end:
        ended = f"ended ({end.get('reason', '?')}) after {end.get('duration_s', 0)}s"
    else:
        ended = "(no call_end — still running or crashed)"
    print("=" * 78)
    print(f"CALL {cid}   started {recs[0]['ts']}   {ended}")
    if start:
        print(f"config: stt={start.get('stt_model')} threads={start.get('stt_threads')} beam={start.get('stt_beam_size')} "
              f"chunk target/max={start.get('chunk_target_s')}/{start.get('chunk_max_s')}s "
              f"llm every {start.get('llm_cadence_s')}s max_tokens={start.get('llm_max_tokens')}")

    # What each LLM review said about each segment
    reviewed = {}
    for r in llm:
        verdict = r.get("verdict")
        for sid in r.get("segments", []):
            if r.get("error"):
                reviewed[sid] = "LLM error"
            elif verdict is None:
                reviewed[sid] = "LLM invalid reply"
            elif verdict["segment_id"] == sid:
                reviewed[sid] = f"LLM {verdict['risk']} ({verdict['speaker']})"
            else:
                reviewed[sid] = "LLM none"
    first_alert = {}
    for a in alerts:
        first_alert.setdefault(a["segment_id"], a)

    print("\nSEGMENTS  (rule result | LLM verdict | alert latency)")
    for s in segs:
        sid = s["segment_id"]
        rule = rules.get(sid, {})
        rule_txt = rule.get("level", "-") + (f"/{rule['label']}" if rule.get("label") else "")
        if rule.get("shadow") and rule.get("level", "none") != "none":
            rule_txt += " (shadow)"
        a = first_alert.get(sid)
        alert_txt = ""
        if a:
            lat = a.get("since_cut_s")
            alert_txt = f"ALERT {a['risk']} via {a['source']}" + (f" +{lat:.1f}s" if lat is not None else "")
        print(f"  {sid:>4} [{s['start_ms'] / 1000:5.1f}s] {s['text']!r}")
        print(f"        rule={rule_txt:<14} {reviewed.get(sid, 'LLM not yet reviewed'):<24} {alert_txt}")

    print("\nSTT      ", stats([r["stt_s"] for r in stt]), "(decode time)")
    print("  rtf    ", stats([r["rtf"] for r in stt], ""), f"| chunks with rtf>1: {sum(r['rtf'] > 1 for r in stt)}/{len(stt)}")
    print("  queue  ", stats([r["queue_wait_s"] for r in stt]), "(chunk cut -> decode start)")
    print("  commit ", stats([s["since_cut_s"] for s in segs]), "(chunk cut -> text available)")
    print("LLM      ", stats([r["duration_s"] for r in llm]),
          f"| errors: {sum(1 for r in llm if r.get('error'))} | invalid: {sum(1 for r in llm if not r.get('error') and r.get('verdict') is None)}")
    timed = [r["timings"] for r in llm if r.get("timings")]
    if timed:
        print("  prompt ", stats([t["prompt_ms"] / 1000 for t in timed if "prompt_ms" in t]),
              f"| tokens in: {[t.get('prompt_n') for t in timed]}")
        print("  decode ", stats([t["predicted_ms"] / 1000 for t in timed if "predicted_ms" in t]),
              f"| tokens out: {[t.get('predicted_n') for t in timed]}")
    verdicts = defaultdict(int)
    for r in llm:
        if r.get("verdict"):
            verdicts[r["verdict"]["risk"]] += 1
    print("  verdicts", dict(verdicts))
    print("ALERTS   ", stats([a["since_cut_s"] for a in alerts if a.get("since_cut_s") is not None and a["revision"] == 1]),
          "(first alert per segment, chunk cut -> shown)")
    for a in alerts:
        print(f"  {a['segment_id']:>4} rev{a['revision']} {a['risk']:<5} via {a['source']:<4} "
              f"speaker={a['speaker']:<7} {a.get('since_cut_s')}s  {a['evidence']!r}")
    if errors:
        print("ERRORS   ", [e.get("message") or e.get("error") for e in errors])
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(DEFAULT_LOG))
    ap.add_argument("--call")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if not Path(args.path).exists():
        sys.exit(f"No log at {args.path}")
    calls, order = load(args.path)
    order = [c for c in order if c]
    if not order:
        sys.exit("Log is empty")
    pick = order if args.all else [args.call or order[-1]]
    for cid in pick:
        if cid not in calls:
            sys.exit(f"Call {cid} not in log. Calls: {', '.join(order)}")
        report(cid, calls[cid])


if __name__ == "__main__":
    main()
