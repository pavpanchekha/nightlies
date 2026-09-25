#!/usr/bin/env python3

from pathlib import Path
import sys
import unittest
from unittest import mock

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

    def test_make_output_reports_invalid_spec(self) -> None:
        output = slack.make_output(
            {"workspace": {"token": "xoxb-token"}},
            "foo",
            "repo",
        )
        self.assertIsNotNone(output)
        assert output is not None
        self.assertEqual(output.error, "Invalid Slack spec: 'foo'")

    def test_invalid_output_does_not_post(self) -> None:
        output = slack.make_output({}, "foo", "repo")
        self.assertIsNotNone(output)
        assert output is not None
        with mock.patch.object(slack, "send_api") as send_api:
            output.post("main", {"result": "success"})
        send_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
