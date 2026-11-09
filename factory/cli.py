"""Developer CLI.

  python -m factory.cli demo            # full thin vertical slice (spec -> agents -> CI -> approval -> nonprod)
  python -m factory.cli demo-rag        # bounded RAG agent + evaluation gate
  python -m factory.cli serve [--port]  # control-plane API + /dashboard
  python -m factory.cli policy-test     # run the executable policy suite
  python -m factory.cli verify-evidence # verify the hash-chained ledger
  python -m factory.cli audit REL-...   # reconstruct a release for auditors
  python -m factory.cli triage          # ops triage agent proposals from sanitized telemetry
"""
from __future__ import annotations

import argparse
import json
import sys

import yaml


def main(argv=None):
    ap = argparse.ArgumentParser(prog="factory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    sub.add_parser("demo-rag")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8088); s.add_argument("--host", default="127.0.0.1")
    sub.add_parser("policy-test")
    sub.add_parser("verify-evidence")
    a = sub.add_parser("audit"); a.add_argument("release_id")
    sub.add_parser("triage")
    args = ap.parse_args(argv)

    if args.cmd == "demo":
        from .demo import main as demo
        demo()
    elif args.cmd == "demo-rag":
        from .factory import Factory
        from .product.rag_app import RagApp, load_corpus
        from .evals.harness import evaluate, load_suite, compare
        f = Factory(); f.bootstrap(); load_corpus(f)
        app = RagApp(f, "1.0.0")
        for q, g in [("What is the maximum hold without supervisor approval?", ["ops-analysts", "retail-all"]),
                     ("What is the maximum hold without supervisor approval?", ["retail-all"])]:
            a = app.ask("ana" if "ops-analysts" in g else "rob", g, q)
            print(f"[{','.join(g)}] {q}\n  -> {a.status}: {a.answer} {a.citations}")
        suite = load_suite()
        base, cand = evaluate(f, app, suite), evaluate(f, RagApp(f, "1.1.0"), suite)
        print("baseline 1.0.0:", base["average"], base["gate"], "| candidate 1.1.0:", cand["average"], cand["gate"],
              cand["critical_blocking"])
        print("decision:", compare(f, cand, base)["decision"])
    elif args.cmd == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.cmd == "policy-test":
        from .core.policy import PolicyEngine
        from .core.util import REPO_ROOT
        pe = PolicyEngine.load(REPO_ROOT / "catalog/policy/factory-core.yaml")
        fails = pe.run_tests(yaml.safe_load((REPO_ROOT / "catalog/policy/factory-core.tests.yaml").read_text()))
        print("PASS" if not fails else "\n".join(fails)); sys.exit(1 if fails else 0)
    elif args.cmd == "verify-evidence":
        from .factory import Factory
        ok, n = Factory().ledger.verify_chain()
        print(f"evidence chain valid={ok} records={n}"); sys.exit(0 if ok else 1)
    elif args.cmd == "audit":
        from .factory import Factory
        from .ops.audit import reconstruct
        r = reconstruct(Factory(), args.release_id); r.pop("chain")
        print(json.dumps(r, indent=2, default=str))
    elif args.cmd == "triage":
        from .factory import Factory
        from .ops.dashboard import snapshot
        f = Factory(); f.bootstrap()
        r = f.agent("ops-triage").run(None, {"kind": "ops.triage", "metrics": f.tm.summary()["delivery"], "threshold": 1})
        print(json.dumps(r.payload, indent=2))


if __name__ == "__main__":
    main()
