"""Owner: metrics member. SQLite records and per-session aggregation."""


def record_result(record: dict) -> dict:
    """Store once by request_id and return this session's updated summary."""
    raise NotImplementedError
