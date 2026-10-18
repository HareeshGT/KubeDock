from webapp.operation_audit import list_operations, record_operation


def test_audit_records_metadata_without_output_fields(tmp_path):
    db_path = tmp_path / "operations.sqlite3"
    record_operation(
        username="tester", category="kubernetes", action="scale",
        target="deployment/default/api", status="succeeded", duration_ms=12,
        db_path=db_path,
    )
    records = list_operations(db_path=db_path)
    assert len(records) == 1
    assert records[0]["username"] == "tester"
    assert records[0]["action"] == "scale"
    assert records[0]["target"] == "deployment/default/api"
    assert records[0]["status"] == "succeeded"
    assert records[0]["duration_ms"] == 12
    assert "output" not in records[0]
    assert "command" not in records[0]


def test_audit_bounds_limit_and_rejects_invalid_status(tmp_path):
    db_path = tmp_path / "operations.sqlite3"
    for index in range(3):
        record_operation(
            username="tester", category="kubernetes", action="restart",
            target=f"deployment/default/api-{index}", status="failed",
            duration_ms=0, error_code=502, db_path=db_path,
        )
    assert len(list_operations(limit=0, db_path=db_path)) == 1
    assert len(list_operations(limit=999, db_path=db_path)) == 3
    try:
        record_operation(username="tester", category="kubernetes", action="test",
                         target="x", status="unknown", duration_ms=0, db_path=db_path)
    except ValueError as exc:
        assert "status" in str(exc)
    else:
        raise AssertionError("invalid status should be rejected")
