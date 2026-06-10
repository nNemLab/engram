import json
from pathlib import Path
from types import SimpleNamespace

from engram.rag.grounding import classify

PROBES = Path(__file__).resolve().parents[1] / "fixtures" / "grounding" / "probes.jsonl"
G = SimpleNamespace(tau_high=0.62, tau_low=0.45, delta=0.08)


def test_default_thresholds_match_labeled_probes():
    failures = []
    for line in PROBES.read_text().splitlines():
        if not line.strip():
            continue
        p = json.loads(line)
        hits = []
        if p["top"] is not None:
            hits.append(SimpleNamespace(hash="a", dense_sim=p["top"]))
            hits.append(SimpleNamespace(hash="b", dense_sim=p["second"]))
        got = classify(hits, G)
        if got != p["expect"]:
            failures.append(f"{p['name']}: expected {p['expect']} got {got}")
    assert not failures, "calibration drift: " + "; ".join(failures)
