"""Protect technical fragments while retaining the sentence context."""
import re


PROTECTED_TOKEN = re.compile(
    r"https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|`[^`\n]+`|"
    r"\b[\w]+(?:\.[\w]+){2,}\b|\b[0-9a-fA-F]{32,}\b|"
    r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")


def split_trailing_punctuation(token):
    """Separate prose punctuation; retain balanced URL parentheses/brackets."""
    end = len(token)
    pairs = {")": "(", "]": "[", "}": "{"}
    while end:
        char = token[end - 1]
        if char in ".,;:!?\"'»”":
            end -= 1
        elif char in pairs and token[:end].count(char) > token[:end].count(pairs[char]):
            end -= 1
        else:
            break
    return token[:end], token[end:]


def mask_tokens(source):
    """Short numeric markers survive Marian better than invented XML tags.

    Each marker is absent from the original source. Restoration still verifies
    every occurrence: inference can alter or omit even a numeric marker.
    """
    mapping = {}
    number = 314159

    def replace(match):
        nonlocal number
        while str(number) in source or str(number) in mapping:
            number += 1
        marker = str(number)
        number += 1
        token = match.group()
        suffix = ""
        if token.startswith(("https://", "http://")):
            token, suffix = split_trailing_punctuation(token)
        mapping[marker] = token
        return marker + suffix

    return PROTECTED_TOKEN.sub(replace, source), mapping


def restore_tokens(translation, mapping):
    if not mapping:
        return translation
    pattern = re.compile(r"(?<!\d)(?:" + "|".join(map(re.escape, mapping)) + r")(?!\d)")
    matches = list(pattern.finditer(translation))
    counts = {marker: 0 for marker in mapping}
    for match in matches:
        counts[match.group()] += 1
    if any(count != 1 for count in counts.values()):
        raise ValueError("Inference damaged or omitted protected fragment markers")
    # One simultaneous substitution: a restored token may contain another marker.
    return pattern.sub(lambda match: mapping[match.group()], translation)
