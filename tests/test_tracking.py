import json
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

from evaluation.tracking import import_report


@unittest.skipUnless(find_spec("mlflow"), "optional MLflow environment")
class TrackingImportTests(unittest.TestCase):
    def test_same_task_in_two_modes_and_interrupted_import(self):
        from mlflow import MlflowClient

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = MlflowClient(tracking_uri=f"sqlite:///{root / 'tracking.db'}")
            experiment = client.create_experiment("comparison", (root / "artifacts").as_uri())
            rows = [{"task": "claim", "customer_mode": mode, "status": status, "checks": [
                {"criterion": "Turn 1 · Outstanding amount belongs to the disputed charge", "passed": False},
                {"criterion": "Turn 2 · Exact committed ledger writes", "passed": True}],
                "dialogue": [{"speaker": "customer", "text": "Check the refund"}]}
                for mode, status in (("llm", "FAIL"), ("scripted", "ERROR"))]
            report = root / "report.json"
            report.write_text(json.dumps(rows))
            self.assertEqual(import_report(client, experiment, report), (2, 0))
            by_mode = {r.data.params["customer_mode"]: r for r in client.search_runs([experiment])}
            completed = by_mode["llm"]
            self.assertEqual(completed.info.status, "FINISHED")
            self.assertEqual(completed.data.metrics["initial_amount_correct"], 0)
            self.assertEqual(completed.data.metrics["committed_writes_correct"], 1)
            self.assertEqual(by_mode["scripted"].info.status, "FAILED")
            self.assertNotIn("initial_amount_correct", by_mode["scripted"].data.metrics)
            client.delete_tag(completed.info.run_id, "export_complete")
            self.assertEqual(import_report(client, experiment, report), (1, 1))
            self.assertEqual(import_report(client, experiment, report), (0, 2))
            self.assertEqual(len(client.search_runs([experiment])), 2)


    def test_historical_customer_and_bank_turn_share_one_native_trace(self):
        from mlflow import MlflowClient

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = MlflowClient(tracking_uri=f"sqlite:///{root / 'tracking.db'}")
            experiment = client.create_experiment("turns", (root / "artifacts").as_uri())

            def audit(path, rows):
                with path.open("w") as out:
                    for seq, (second, kind, component, payload, call) in enumerate(rows, 1):
                        event = {"sequence": seq, "timestamp": f"2026-01-01T00:00:{second:02}+00:00",
                                 "run_id": path.stem, "event_type": kind, "component": component,
                                 "payload": payload, "call_id": call}
                        out.write(json.dumps(event) + "\n")

            (root / "customers").mkdir()
            audit(root / "customers/one.jsonl", [
                (0, "model.requested", "customer", {"tools": {}}, None),
                (1, "model.responded", "customer", {"text": "Refund?"}, None)])
            audit(root / "bank.jsonl", [
                (2, "run.started", "harness", {"message": "Refund?", "conversation_id": "session123"}, None),
                (3, "tool.requested", "harness", {"name": "lookup", "arguments": {"ref": "abc"}}, "K"),
                (4, "tool.started", "harness", {"name": "lookup"}, "K"),
                (5, "tool.completed", "lookup", {"value": 64}, "K"),
                (6, "run.finished", "harness", {"reply": "Outstanding £64", "reason": "final_reply"}, None)])
            audit(root / "customers/two.jsonl", [
                (7, "model.requested", "customer", {}, None),
                (8, "generation.failed", "customer", {"error": "timeout"}, None)])
            report = root / "report.json"
            report.write_text(json.dumps([{"task": "refund", "status": "ERROR", "checks": [],
                "customer_mode": "llm", "turn_audits": ["bank.jsonl"],
                "customer_audits": ["customers/one.jsonl", "customers/two.jsonl"]}]))
            import_report(client, experiment, report)
            run_id = client.search_runs([experiment])[0].info.run_id
            client.delete_tag(run_id, "turn_traces_version")
            import_report(client, experiment, report)
            traces = client.search_traces(locations=[experiment], run_id=run_id)
            self.assertEqual(len(traces), 2)
            self.assertTrue(all(t.info.trace_metadata["mlflow.trace.session"] == "session123" for t in traces))
            turns = {t.info.tags["turn"]: t for t in traces}
            spans = {s.name: s for s in turns["1"].data.spans}
            self.assertEqual(spans["lookup"].inputs["arguments"], {"ref": "abc"})
            self.assertEqual(spans["lookup"].outputs["value"], 64)
            self.assertEqual(spans["lookup"].end_time_ns - spans["lookup"].start_time_ns, 10**9)
            self.assertEqual(spans["customer model"].parent_id, spans["lookup"].parent_id)
            self.assertEqual(turns["2"].info.state.value, "ERROR")
