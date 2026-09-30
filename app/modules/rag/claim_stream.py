"""Pull finished claims out of a streamed structured answer.

The model streams one JSON object: {"claims": [{...}, {...}], "insufficient_evidence": ..., "conflicts": [...]}.
`ClaimStream.feed` returns each claim object as soon as its closing brace has
arrived, so it can be validated and shown while later claims are still being
written. Braces and brackets inside strings (and escaped quotes) are ignored.
The complete text is still parsed and validated as a whole at the end; this
only decides when a claim is ready, never what it says.
"""

import json
import re

_CLAIMS_START = re.compile(r'"claims"\s*:\s*\[')


class ClaimStream:
    def __init__(self) -> None:
        self._text = ""
        self._position = 0  # next character to scan
        self._in_array = False
        self._done = False
        self._depth = 0  # object nesting inside the claims array
        self._in_string = False
        self._escaped = False
        self._object_start = -1

    def feed(self, delta: str) -> list[dict]:
        """Add streamed text; return the claims that became complete."""
        self._text += delta
        if self._done:
            return []
        if not self._in_array:
            match = _CLAIMS_START.search(self._text)
            if not match:
                return []
            self._in_array = True
            self._position = match.end()
        finished: list[dict] = []
        text = self._text
        index = self._position
        while index < len(text):
            char = text[index]
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    self._in_string = False
            elif char == '"':
                self._in_string = True
            elif char == "{":
                if self._depth == 0:
                    self._object_start = index
                self._depth += 1
            elif char == "}":
                self._depth -= 1
                if self._depth == 0 and self._object_start >= 0:
                    try:
                        claim = json.loads(text[self._object_start:index + 1])
                    except json.JSONDecodeError:
                        claim = None
                    if isinstance(claim, dict):
                        finished.append(claim)
                    self._object_start = -1
            elif char == "]" and self._depth == 0:
                self._done = True
                index += 1
                break
            index += 1
        self._position = index
        return finished
