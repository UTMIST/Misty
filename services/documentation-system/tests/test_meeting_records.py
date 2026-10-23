import pytest

from src.storage.in_memory import InMemoryStorageAdapter
from tests.conftest import build_seed_sources
from tests.meeting_record_cases import MeetingRecordCases


class TestMeetingRecords(MeetingRecordCases):
    @pytest.fixture
    def adapter(self):
        return InMemoryStorageAdapter(build_seed_sources())
