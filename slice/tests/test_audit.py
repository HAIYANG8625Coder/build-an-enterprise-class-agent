"""R5: a changed log shows."""
import json
import os
import tempfile
import unittest

import helpers
from audit import AuditError, AuditLog, line_hash, verify


class Audit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder, self.anchor = self.tmp.name + "/audit", self.tmp.name + "/anchor"
        self.clock = helpers.FakeClock()
        self.clock.seconds = 1_800_000_000
        self.log = AuditLog(self.folder, self.anchor, self.clock)
        for n in range(6):
            if n == 3:
                self.clock.seconds += 86400                   # the chain goes on into the next day's file
            self.log.append(type="tool_decision", user="lin@acme", decision="allow", source="service")
        self.files = sorted(os.path.join(self.folder, f) for f in os.listdir(self.folder))

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self, i):
        with open(self.files[i]) as f:
            return f.readlines()

    def write(self, i, lines):
        with open(self.files[i], "w") as f:
            f.writelines(lines)

    def check(self):
        return verify(self.folder, self.anchor)

    def test_a_whole_log_checks_out_across_days(self):
        self.assertEqual(len(self.files), 2)
        self.assertEqual((self.check()["ok"], self.check()["checked"]), (True, 6))

    def test_an_edited_line_is_named(self):
        lines = self.lines(0)
        lines[1] = lines[1].replace('"allow"', '"deny"')
        self.write(0, lines)
        self.assertEqual((self.check()["ok"], self.check()["broken_at"]), (False, 2))

    def test_a_removed_line_is_named(self):
        lines = self.lines(0)
        del lines[1]
        self.write(0, lines)
        self.assertEqual((self.check()["ok"], self.check()["broken_at"]), (False, 2))

    def test_lines_cut_off_the_end_show_through_the_anchor(self):
        self.write(1, self.lines(1)[:-1])
        self.assertEqual((self.check()["ok"], self.check()["broken_at"]), (False, 6))

    def test_a_whole_new_chain_shows_through_the_anchor(self):
        prev, count = "0" * 64, 0
        for i in range(2):
            new = []
            for line in self.lines(i):
                event = json.loads(line)
                event.update(user="someone@else", prev=prev)
                event["hash"] = prev = line_hash(event)
                new.append(json.dumps(event, sort_keys=True) + "\n")
                count += 1
            self.write(i, new)
        result = self.check()
        self.assertEqual((result["ok"], result["checked"], result["broken_at"]), (False, 6, 6))
        self.assertIn("anchor", result["why"])

    def test_junk_and_a_missing_anchor(self):
        os.remove(self.anchor)
        self.assertFalse(self.check()["ok"])
        self.write(0, ["not json\n"])
        self.assertEqual(self.check()["broken_at"], 1)

    def test_a_restart_continues_and_refuses_a_broken_log(self):
        again = AuditLog(self.folder, self.anchor, self.clock)
        self.assertEqual(again.append(type="request", source="service"), 7)
        self.write(0, self.lines(0)[1:])
        with self.assertRaises(AuditError):
            AuditLog(self.folder, self.anchor, self.clock)

    def test_there_is_no_field_for_content(self):
        with self.assertRaises(AuditError):
            self.log.append(type="request", content="a prompt")


if __name__ == "__main__":
    unittest.main()
