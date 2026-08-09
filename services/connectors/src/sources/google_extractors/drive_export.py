"""Generic fallback: Drive export for Google-native types, media download for
uploaded text files. Lossy by nature — it flattens all structure — so a native
API extractor should replace it per type where the structure matters.
"""

from src.sources.google_extractors.base import ExtractedText, execute

DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"


class DriveExportExtractor:
    scopes = (DRIVE_READONLY,)
    services = ("drive",)

    def __init__(self, *, export_mime: str | None, warning: str | None = None) -> None:
        # export_mime=None means the file has real bytes to download
        # (alt="media") rather than a Google-native type needing export.
        self._export_mime = export_mime
        self._warning = warning

    def extract(self, services: dict, file_id: str, mime: str) -> ExtractedText:
        files = services["drive"].files()
        if self._export_mime is None:
            payload = execute(files.get(fileId=file_id, alt="media"))
        else:
            payload = execute(files.export(fileId=file_id, mimeType=self._export_mime))
        text = (
            payload.decode("utf-8", errors="replace")
            if isinstance(payload, bytes)
            else str(payload)
        )
        return ExtractedText(text=text, warnings=[self._warning] if self._warning else [])
