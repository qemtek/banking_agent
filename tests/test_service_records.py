import tempfile
import unittest
from pathlib import Path

from bank_demo import Session
from bank_demo.ledger import Ledger, statement
from bank_demo.service_records import ServiceRecords, ServiceTools


class ServiceRecordTests(unittest.TestCase):
    def test_seeded_noise_and_tool_access_survive_reopen(self):
        data = statement(41)
        records = ServiceRecords.generate(41, data)
        self.assertEqual(records, ServiceRecords.generate(41, data))
        self.assertNotEqual(records, ServiceRecords.generate(907, statement(907)))
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp), data, draft=True)
            alice, bob = Session("alice"), Session("bob")
            draft = ledger.read(alice)
            records.install(ledger)
            bank = Ledger.reopen(ledger.path)
            tools = ServiceTools(bank).tools()
            self.assertEqual(bank.read(alice), draft)
            activity = bank.transactions(alice)["transactions"]
            original = {r[0] for r in data.rows if r[1] == "alice"}
            self.assertEqual(len(activity), len(original) + 20)
            own_refs = {r["reference"] for r in activity}
            self.assertTrue(all(r.get("refund_for") in own_refs for r in activity if "refund_for" in r))
            self.assertEqual({r["reference"] for r in activity if r["merchant"] == "North Pier"
                              and r["amount"] == "-64.00"}, {data.first, data.target})
            requests = tools["list_service_requests"].execute
            payments = tools["list_scheduled_payments"].execute
            for session in (alice, bob):
                self.assertEqual(len(requests(session)["requests"]), 4)
                self.assertEqual(len(payments(session)["payments"]), 3)
            own = requests(alice)["requests"][0]["ref"]
            foreign = requests(bob)["requests"][0]["ref"]
            self.assertNotIn(foreign, str(requests(alice)))
            self.assertNotIn(payments(bob)["payments"][0]["reference"], str(payments(alice)))
            read = tools["read_service_request"].execute
            self.assertEqual(read(alice, own)["ref"], own)
            with self.assertRaises(ValueError):
                read(alice, foreign)
