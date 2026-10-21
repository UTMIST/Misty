"""Drive identity in supported URLs, also usable by Postgres substring().

Keep the single capture group in each expression: SQL substring returns it.
Published editor/Forms response ids are intentionally not Drive file identities.
"""

import re

GOOGLE_ID_PATTERNS = (
    r"^https?://docs\.google\.com/(?:document|spreadsheets|presentation)/d/"
    r"(?!e/)([A-Za-z0-9_-]+)(?:[/?#]|$)",
    r"^https?://docs\.google\.com/forms/d/(?!e/)([A-Za-z0-9_-]+)(?:[/?#]|$)",
    r"^https?://drive\.google\.com/file/d/([A-Za-z0-9_-]+)(?:[/?#]|$)",
    r"^https?://drive\.google\.com/open\?(?:[^#]*&)?id=([A-Za-z0-9_-]+)(?:[&#]|$)",
)


def google_file_id(url: str) -> str | None:
    for pattern in GOOGLE_ID_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None
