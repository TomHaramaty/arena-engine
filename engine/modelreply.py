"""Reading a model's reply when the model has added something to it.

Three places in this engine ask a model for JSON and get JSON plus something
else: an operations block with a second fence after it (runner/ops.parse), a
letter's prose (jobs/letter), and a weekly reflection (runner/reflect). All
three learned it separately, and the third one learned it the expensive way —
`runner/reflect.call_pro` was still calling a bare `json.loads` on 2026-07-31
when the model returned a complete, valid reflection followed by trailing
content. `json.decoder.JSONDecodeError: Extra data: line 40 column 1` dropped
ballast's weekly reflection, and nothing retries a reflection.

Asking for `response_mime_type: application/json` does not make this go away.
That setting constrains the reply to CONTAIN valid JSON; it does not promise
the reply is NOTHING BUT JSON.

The tolerance here is deliberately narrow, and the line it holds is the one
that matters: reading a COMPLETE object that arrived with a tail is not the
same act as repairing an incomplete one. A truncated reply still raises, and a
failed reflection is still a failed reflection.
"""
import json
import re


def first_json_object(text: str) -> dict:
    """The first complete JSON object in a model reply.

    Tolerates a code fence around it and anything after it. Scans for the first
    balanced object rather than trusting the reply's boundaries; anything
    genuinely malformed still raises.
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\s*|\s*```$", "", s, flags=re.I | re.S).strip()
    start = s.find("{")
    if start < 0:
        raise ValueError("no json object in the model reply")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(s[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("unterminated json object in the model reply")
