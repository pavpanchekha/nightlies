#!/usr/bin/env python3

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import slack


class TestSlackSpec(unittest.TestCase):
    def test_parse_slack_spec_uses_bot_token_and_channel(self) -> None:
        token, channel = slack.parse_slack_spec(
            "workspace/results",
            {"workspace": {"token": "xoxb-token"}},
        )
        self.assertEqual(token, "xoxb-token")
        self.assertEqual(channel, "results")

    def test_parse_slack_spec_rejects_webhook_key(self) -> None:
        with self.assertRaisesRegex(slack.SlackError, "Invalid Slack spec"):
            slack.parse_slack_spec(
                "legacy",
                {"legacy": {"slack": "https://hooks.slack.com/services/old"}},
            )


if __name__ == "__main__":
    unittest.main()
