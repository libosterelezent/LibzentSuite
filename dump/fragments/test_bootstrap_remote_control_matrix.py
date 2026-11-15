# RETIRED 2026-11-15 from tests/unit/test_bootstrap_fixes.py (verbatim)

# --------------------------------------------------- REMOTE_CONTROL matrix
class TestRemoteControlMatrix(unittest.TestCase):
    def test_absent_key_defaults_on(self):
        self.assertIs(Credentials({}).remote_control_enabled, True)

    def test_matrix(self):
        cases = {
            "0": False, "disable": False, "": False,
            "false": False, "OFF": False,
            "1": True, "on": True, "yes": True,
        }
        for raw, expected in cases.items():
            creds = Credentials({"REMOTE_CONTROL": raw})
            self.assertIs(creds.remote_control_enabled, expected,
                          f"REMOTE_CONTROL={raw!r}")


