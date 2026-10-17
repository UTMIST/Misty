from src.api.app import create_app


def test_openapi_includes_meeting_paths():
    schema = create_app().openapi()
    paths = schema["paths"]

    assert "/meetings/{session_id}/transcript" in paths
    assert "get" in paths["/meetings/{session_id}/transcript"]
    assert "/meetings/{session_id}/stop" in paths
    assert "post" in paths["/meetings/{session_id}/stop"]
    assert "post" in paths["/meetings/{session_id}/qna"]
