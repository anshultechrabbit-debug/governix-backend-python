"""Pull finished claims out of a streamed structured answer.

The model streams one JSON object: {"insufficient_evidence": ..., "claims": [{...}, {...}], "conflicts": [...]}.
`ClaimStream.feed` returns each claim object as soon as its closing brace has
arrived, so it can be validated and shown while later claims are still being
written. Braces and brackets inside strings (and escaped quotes) are ignored.
The complete text is still parsed and validated as a whole at the end; this
only decides when a claim is ready, never what it says.
"""

import json
import re

_CLAIMS_START = re.compile(r'"claims"\s*:\s*\[')
_INSUFFICIENT = re.compile(r'"insufficient_evidence"\s*:\s*true')


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

    @property
    def declared_insufficient(self) -> bool:
        """The model said the evidence does not answer the question (it writes this before the claims)."""
        return bool(_INSUFFICIENT.search(self._text))

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
                    # _object_start is reset here; _depth is already 0 by definition
                    # (we only enter this branch when depth reaches exactly 0).
                    # Do NOT reset _depth: it is already 0, and resetting it would
                    # be both redundant and a signal to future maintainers that a
                    # manual reset is needed — it is not. The invariant is structural:
                    # every { increments and every } decrements, so depth=0 always
                    # means we are outside all open objects.
                    self._object_start = -1
            elif char == "]" and self._depth == 0:
                self._done = True
                index += 1
                break
            index += 1
        self._position = index
        return finished
