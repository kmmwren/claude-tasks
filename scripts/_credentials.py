"""What counts as a credential: a file that carries one is never staged or landed."""

import re

# High-signal credential shapes only: a sweep stages whole files, so one that carries a key stays local.
# The last group is a signed or token-bearing link (AWS presigned, Azure SAS, CloudFront, a bare token
# parameter): it expires, so it must never sit in git history.
_SECRET = re.compile(
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|(?i:[?&]x-amz-(?:signature|credential)="
    r"|(?=\S*[?&](?:se|sp)=)\S*[?&]sig="
    r"|[?&](?:signature|key-pair-id|policy)="
    r"|[?&](?:access_)?token=)"
)


def carries_credential(text: str) -> bool:
    """True when `text` holds a credential or a signed link."""
    return _SECRET.search(text) is not None
