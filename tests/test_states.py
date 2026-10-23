import unittest

from tool_system.states import JobState


class JobStateTests(unittest.TestCase):
    def test_required_states_exist(self):
        self.assertEqual(
            {state.value for state in JobState},
            {"PENDING", "RUNNING", "WAITING", "VERIFIED", "FAILED"},
        )


if __name__ == "__main__":
    unittest.main()
