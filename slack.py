from typing import Dict, Any, List, Optional, Mapping
import json
import urllib.request, urllib.error

class SlackError(Exception):
    pass

class Block:
    def to_json(self) -> Dict[str, Any]:
        raise NotImplementedError

class Accessory:
    def to_json(self) -> Dict[str, Any]:
        raise NotImplementedError

class Response:
    def __init__(self, text: str):
        self.text = text
        self.blocks : List[Block] = []

    def to_json(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "blocks": [block.to_json() for block in self.blocks],
        }

    def add(self, block : Block) -> None:
        assert isinstance(block, Block), "Can only add Blocks to Response"
        self.blocks.append(block)

class TextBlock(Block):
    def __init__(self, text : str):
        self.text = text
        self.accessory : Optional[Accessory] = None

    def to_json(self) -> Dict[str, Any]:
        block = {
            "type": "section",
            "text": { "type": "mrkdwn", "text": self.text },
        }
        if self.accessory:
            block["accessory"] = self.accessory.to_json()
        return block

    def add(self, accessory: Accessory) -> None:
        assert isinstance(accessory, Accessory), "Can only add Accessories to Blocks"
        assert not self.accessory, "This Block already has an Accessory"
        self.accessory = accessory
        
class Image(Block):
    def __init__(self, text : str, url : str):
        self.text = text
        self.url = url

    def to_json(self) -> Dict[str, Any]:
        return {
            "type": "image",
            "image_url": self.url,
            "alt_text": self.text,
        }

class Button(Accessory):
    key = "accessory"

    def __init__(self, text : str, url : str, style : Optional[str] = None):
        assert style in { None, "primary", "danger" }
        self.text = text
        self.url = url
        self.style = style

    def to_json(self) -> Dict[str, Any]:
        accessory = {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": self.text,
            },
            "url": self.url,
        }
        if self.style:
            accessory["style"] = self.style
        return accessory

def build_runs(
    name : str,
    branch : str,
    info : Dict[str, str],
    warnings : Optional[Dict[str, str]] = None,
) -> Response:
    res = Response(f"Nightly data for {name}")
    if warnings:
        for key in sorted(warnings):
            res.add(TextBlock(f":warning: {warnings[key]}"))
    
    result = info["result"]
    time = info.get("time")
    if time:
        text = f"Branch `{branch}` of `{name}` was a {result} in {time}"
    else:
        text = f"Branch `{branch}` of `{name}` was {result} before starting"
    block = TextBlock(text)

    if "success" != result and info.get("logurl"):
        btn = Button("Error log", info["logurl"], style="primary")
        block.add(btn)
    elif "url" in info:
        btn = Button("View Report", info["url"])
        block.add(btn)

    res.add(block)

    if "img" in info:
        url, *alttext = info["img"].split(" ")
        text = " ".join(alttext) or f"Image for {name} branch {branch}"
        img = Image(text, url)
        res.add(img)
    return res
    
def build_fatal(name : str, text : str) -> Response:
    res = Response(f"Fatal error running nightlies for {name}")
    res.add(TextBlock(text))
    return res

SLACK_API_URL = "https://slack.com/api/chat.postMessage"

def parse_slack_spec(
    spec: str,
    secrets: Mapping[str, Mapping[str, str]],
) -> tuple[str, str]:
    if "/" not in spec:
        raise SlackError(f"Invalid Slack spec: {spec!r}")
    workspace, channel = spec.split("/", 1)
    if workspace not in secrets:
        raise SlackError(f"Unknown Slack workspace: {workspace}")
    token = secrets[workspace].get("token")
    if not token:
        raise SlackError(f"Missing token for Slack workspace: {workspace}")
    return token, channel

def send_api(token: str, channel: str, res: Response) -> None:
    payload = res.to_json()
    payload["channel"] = channel
    data = json.dumps(payload).encode("utf8")
    req = urllib.request.Request(SLACK_API_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf8")
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        reason = exc.read().decode("utf-8", errors="replace")
        raise SlackError(f"{exc.code} {exc.reason}: {reason}")
    try:
        reply = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SlackError(f"Invalid Slack response: {exc}") from exc
    if not reply.get("ok", False):
        error = reply.get("error", "unknown_error")
        raise SlackError(f"Slack API error: {error}")

class SlackOutput:
    def __init__(self, secrets: Mapping[str, Mapping[str, str]], spec: str, name: str):
        token, channel = parse_slack_spec(spec, secrets)
        self.token = token
        self.channel = channel
        self.name = name
        self.warnings: Dict[str, str] = {}

    def warn(self, key: str, message: str) -> None:
        self.warnings[key] = message

    def fatal(self, message: str) -> None:
        data = build_fatal(self.name, message)
        send_api(self.token, self.channel, data)

    def post(self, branch: str, info: Dict[str, str]) -> None:
        data = build_runs(self.name, branch, info, self.warnings if self.warnings else None)
        send_api(self.token, self.channel, data)
        self.warnings.clear()

    def post_warnings(self) -> None:
        if not self.warnings:
            return
        res = Response(f"Warnings for {self.name}")
        for key in sorted(self.warnings):
            res.add(TextBlock(f":warning: {self.warnings[key]}"))
        send_api(self.token, self.channel, res)
        self.warnings.clear()


def make_output(secrets: Mapping[str, Mapping[str, str]], spec: Optional[str], name: str) -> Optional[SlackOutput]:
    if not spec:
        return None
    return SlackOutput(secrets, spec, name)
