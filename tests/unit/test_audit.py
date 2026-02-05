"""Unit tests for reconcile audit module.

Tests:
- AuditConfig: defaults, env var overrides
- AuditEvent: serialization, schema
- AuditWriter: append-only, rotation, redaction
- Factory functions: event creation
- Determinism: same inputs → same outputs
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grinder.reconcile.audit import (
    DEFAULT_AUDIT_PATH,
    DEFAULT_MAX_BYTES,
    ENV_AUDIT_ENABLED,
    ENV_AUDIT_PATH,
    REDACTED_FIELDS,
    AuditConfig,
    AuditEvent,
    AuditEventType,
    AuditWriteError,
    AuditWriter,
    create_reconcile_run_event,
    create_remediate_attempt_event,
    create_remediate_result_event,
)
from grinder.reconcile.config import ReconcileConfig, RemediationAction
from grinder.reconcile.remediation import RemediationExecutor
from grinder.reconcile.runner import ReconcileRunner

# =============================================================================
# AuditConfig Tests
# =============================================================================


class TestAuditConfig:
    """Tests for AuditConfig."""

    def test_defaults(self) -> None:
        """Default config has safe values."""
        config = AuditConfig()
        assert config.enabled is False
        assert config.path == DEFAULT_AUDIT_PATH
        assert config.max_bytes == DEFAULT_MAX_BYTES
        assert config.redact is True
        assert config.fail_open is True

    def test_env_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_AUDIT_ENABLED=1 enables audit."""
        monkeypatch.setenv(ENV_AUDIT_ENABLED, "1")
        config = AuditConfig()
        assert config.enabled is True

    def test_env_enabled_0_does_not_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_AUDIT_ENABLED=0 does not enable audit."""
        monkeypatch.setenv(ENV_AUDIT_ENABLED, "0")
        config = AuditConfig()
        assert config.enabled is False

    def test_env_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_AUDIT_PATH overrides path."""
        monkeypatch.setenv(ENV_AUDIT_PATH, "/custom/path.jsonl")
        config = AuditConfig()
        assert config.path == "/custom/path.jsonl"


# =============================================================================
# AuditEvent Tests
# =============================================================================


class TestAuditEvent:
    """Tests for AuditEvent."""

    def test_to_json_dict(self) -> None:
        """Event converts to JSON-serializable dict."""
        event = AuditEvent(
            ts_ms=1704067200000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="1704067200000_1",
            mode="dry_run",
            action="cancel_all",
            symbols=("BTCUSDT", "ETHUSDT"),
            mismatch_counts={"ORDER_EXISTS_UNEXPECTED": 2},
            details={"key": "value"},
        )
        d = event.to_json_dict()

        assert d["ts_ms"] == 1704067200000
        assert d["event_type"] == "RECONCILE_RUN"
        assert d["run_id"] == "1704067200000_1"
        assert d["mode"] == "dry_run"
        assert d["action"] == "cancel_all"
        assert d["symbols"] == ["BTCUSDT", "ETHUSDT"]  # list, not tuple
        assert d["mismatch_counts"] == {"ORDER_EXISTS_UNEXPECTED": 2}
        assert d["details"] == {"key": "value"}

    def test_to_json_line_deterministic(self) -> None:
        """Same event produces same JSON line (sorted keys)."""
        event = AuditEvent(
            ts_ms=1704067200000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test_run",
            symbols=("BTCUSDT",),
        )
        line1 = event.to_json_line()
        line2 = event.to_json_line()
        assert line1 == line2
        # Verify sorted keys
        assert '"action":' in line1
        assert line1.index('"action":') < line1.index('"event_type":')

    def test_schema_version_default(self) -> None:
        """Schema version defaults to 1."""
        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
        )
        assert event.schema_version == 1

    def test_frozen(self) -> None:
        """Event is immutable."""
        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
        )
        with pytest.raises(AttributeError):
            event.ts_ms = 2000  # type: ignore[misc]


# =============================================================================
# AuditWriter Tests
# =============================================================================


class TestAuditWriter:
    """Tests for AuditWriter."""

    def test_disabled_by_default(self, tmp_path: Path) -> None:
        """Writer does nothing when disabled."""
        config = AuditConfig(enabled=False, path=str(tmp_path / "audit.jsonl"))
        writer = AuditWriter(config)
        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
        )
        result = writer.write(event)
        assert result is False
        assert not (tmp_path / "audit.jsonl").exists()

    def test_write_creates_file(self, tmp_path: Path) -> None:
        """Writer creates file and directories."""
        audit_path = tmp_path / "subdir" / "audit.jsonl"
        config = AuditConfig(enabled=True, path=str(audit_path))
        writer = AuditWriter(config)
        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
        )
        result = writer.write(event)
        writer.close()

        assert result is True
        assert audit_path.exists()

    def test_write_appends_jsonl(self, tmp_path: Path) -> None:
        """Writer appends JSON lines."""
        audit_path = tmp_path / "audit.jsonl"
        config = AuditConfig(enabled=True, path=str(audit_path))
        writer = AuditWriter(config)

        for i in range(3):
            event = AuditEvent(
                ts_ms=1000 + i,
                event_type=AuditEventType.RECONCILE_RUN,
                run_id=f"test_{i}",
            )
            writer.write(event)

        writer.close()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 3

        for i, line in enumerate(lines):
            data = json.loads(line)
            assert data["ts_ms"] == 1000 + i
            assert data["run_id"] == f"test_{i}"

    def test_context_manager(self, tmp_path: Path) -> None:
        """Writer works as context manager."""
        audit_path = tmp_path / "audit.jsonl"
        config = AuditConfig(enabled=True, path=str(audit_path))

        with AuditWriter(config) as writer:
            event = AuditEvent(
                ts_ms=1000,
                event_type=AuditEventType.RECONCILE_RUN,
                run_id="test",
            )
            writer.write(event)

        # File should be closed and flushed
        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_redaction_removes_secrets(self, tmp_path: Path) -> None:
        """Redaction removes sensitive fields."""
        audit_path = tmp_path / "audit.jsonl"
        config = AuditConfig(enabled=True, path=str(audit_path), redact=True)
        writer = AuditWriter(config)

        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
            details={
                "api_key": "secret123",
                "api_secret": "verysecret",
                "normal_field": "visible",
                "nested": {
                    "token": "should_be_redacted",
                    "data": "ok",
                },
            },
        )
        writer.write(event)
        writer.close()

        data = json.loads(audit_path.read_text().strip())
        assert data["details"]["api_key"] == "[REDACTED]"
        assert data["details"]["api_secret"] == "[REDACTED]"
        assert data["details"]["normal_field"] == "visible"
        assert data["details"]["nested"]["token"] == "[REDACTED]"
        assert data["details"]["nested"]["data"] == "ok"

    def test_redaction_disabled(self, tmp_path: Path) -> None:
        """Redaction can be disabled."""
        audit_path = tmp_path / "audit.jsonl"
        config = AuditConfig(enabled=True, path=str(audit_path), redact=False)
        writer = AuditWriter(config)

        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
            details={"api_key": "secret123"},
        )
        writer.write(event)
        writer.close()

        data = json.loads(audit_path.read_text().strip())
        assert data["details"]["api_key"] == "secret123"

    def test_rotation_by_size(self, tmp_path: Path) -> None:
        """Writer rotates file when max_bytes exceeded."""
        audit_path = tmp_path / "audit.jsonl"
        # Small max_bytes to trigger rotation quickly
        config = AuditConfig(enabled=True, path=str(audit_path), max_bytes=200)
        writer = AuditWriter(config)

        # Write enough events to exceed max_bytes
        for i in range(10):
            event = AuditEvent(
                ts_ms=1000 + i,
                event_type=AuditEventType.RECONCILE_RUN,
                run_id=f"test_{i}",
                symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
            )
            writer.write(event)

        writer.close()

        # Should have rotated to .1
        assert audit_path.exists()
        assert Path(f"{audit_path}.1").exists()

    def test_rotation_by_event_count(self, tmp_path: Path) -> None:
        """Writer rotates file when max_events_per_file exceeded."""
        audit_path = tmp_path / "audit.jsonl"
        config = AuditConfig(enabled=True, path=str(audit_path), max_events_per_file=5)
        writer = AuditWriter(config)

        for i in range(8):
            event = AuditEvent(
                ts_ms=1000 + i,
                event_type=AuditEventType.RECONCILE_RUN,
                run_id=f"test_{i}",
            )
            writer.write(event)

        writer.close()

        # Should have rotated
        assert audit_path.exists()
        assert Path(f"{audit_path}.1").exists()

    def test_generate_run_id_deterministic(self) -> None:
        """Run ID is deterministic with injectable clock."""
        config = AuditConfig(enabled=True)
        writer = AuditWriter(config, _clock=lambda: 1704067200000)
        run_id1 = writer.generate_run_id()
        run_id2 = writer.generate_run_id()

        assert run_id1 == "1704067200000_1"
        assert run_id2 == "1704067200000_2"

    def test_generate_run_id_injectable_factory(self) -> None:
        """Run ID factory can be injected for testing."""
        config = AuditConfig(enabled=True)
        writer = AuditWriter(config, _run_id_factory=lambda ts: f"fixed_{ts}")
        run_id = writer.generate_run_id()
        assert run_id.startswith("fixed_")

    def test_fail_open_continues_on_error(self, tmp_path: Path) -> None:
        """Writer continues on error when fail_open=True."""
        # Use a path that will fail (directory instead of file)
        audit_path = tmp_path / "audit.jsonl"
        audit_path.mkdir()  # Create directory, write should fail

        config = AuditConfig(enabled=True, path=str(audit_path), fail_open=True)
        writer = AuditWriter(config)

        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
        )
        result = writer.write(event)
        assert result is False
        assert writer.write_errors >= 1

    def test_fail_closed_raises_error(self, tmp_path: Path) -> None:
        """Writer raises error when fail_open=False."""
        # Use a path that will fail
        audit_path = tmp_path / "audit.jsonl"
        audit_path.mkdir()

        config = AuditConfig(enabled=True, path=str(audit_path), fail_open=False)
        writer = AuditWriter(config)

        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
        )
        with pytest.raises(AuditWriteError):
            writer.write(event)

    def test_event_count_tracking(self, tmp_path: Path) -> None:
        """Writer tracks event count."""
        audit_path = tmp_path / "audit.jsonl"
        config = AuditConfig(enabled=True, path=str(audit_path))
        writer = AuditWriter(config)

        assert writer.event_count == 0

        for i in range(3):
            event = AuditEvent(
                ts_ms=1000 + i,
                event_type=AuditEventType.RECONCILE_RUN,
                run_id=f"test_{i}",
            )
            writer.write(event)

        assert writer.event_count == 3
        writer.close()


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestFactoryFunctions:
    """Tests for event factory functions."""

    def test_create_reconcile_run_event(self) -> None:
        """Factory creates valid RECONCILE_RUN event."""
        event = create_reconcile_run_event(
            run_id="test_run",
            ts_start=1000,
            ts_end=2000,
            mode="dry_run",
            action="cancel_all",
            mismatch_counts={"ORDER_EXISTS_UNEXPECTED": 2},
            symbols=["BTCUSDT", "ETHUSDT"],
            cancel_count=2,
            flatten_count=0,
            executed_count=0,
            planned_count=2,
            blocked_count=0,
            skipped_terminal=1,
            skipped_no_action=0,
        )

        assert event.event_type == AuditEventType.RECONCILE_RUN
        assert event.run_id == "test_run"
        assert event.mode == "dry_run"
        assert event.action == "cancel_all"
        assert event.symbols == ("BTCUSDT", "ETHUSDT")  # sorted, tuple
        assert event.mismatch_counts == {"ORDER_EXISTS_UNEXPECTED": 2}
        assert event.details["cancel_count"] == 2
        assert event.details["duration_ms"] == 1000

    def test_create_reconcile_run_event_bounds_symbols(self) -> None:
        """Factory bounds symbol list to 10."""
        symbols = [f"SYM{i}USDT" for i in range(20)]
        event = create_reconcile_run_event(
            run_id="test",
            ts_start=1000,
            ts_end=2000,
            mode="dry_run",
            action="none",
            mismatch_counts={},
            symbols=symbols,
            cancel_count=0,
            flatten_count=0,
            executed_count=0,
            planned_count=0,
            blocked_count=0,
            skipped_terminal=0,
            skipped_no_action=0,
        )

        assert len(event.symbols) == 10

    def test_create_remediate_attempt_event(self) -> None:
        """Factory creates valid REMEDIATE_ATTEMPT event."""
        event = create_remediate_attempt_event(
            run_id="test_run",
            ts_ms=1500,
            mode="live",
            action="cancel_all",
            symbol="BTCUSDT",
            client_order_id="grinder_default_BTCUSDT_1_1000_1",
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
        )

        assert event.event_type == AuditEventType.REMEDIATE_ATTEMPT
        assert event.run_id == "test_run"
        assert event.ts_ms == 1500
        assert event.mode == "live"
        assert event.action == "cancel_all"
        assert event.symbols == ("BTCUSDT",)
        assert event.details["client_order_id"] == "grinder_default_BTCUSDT_1_1000_1"
        assert event.details["mismatch_type"] == "ORDER_EXISTS_UNEXPECTED"

    def test_create_remediate_result_event(self) -> None:
        """Factory creates valid REMEDIATE_RESULT event."""
        event = create_remediate_result_event(
            run_id="test_run",
            ts_ms=1600,
            mode="dry_run",
            action="cancel_all",
            symbol="BTCUSDT",
            client_order_id="grinder_default_BTCUSDT_1_1000_1",
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
            status="PLANNED",
            block_reason=None,
            error=None,
        )

        assert event.event_type == AuditEventType.REMEDIATE_RESULT
        assert event.status == "PLANNED"
        assert event.block_reason is None

    def test_create_remediate_result_event_blocked(self) -> None:
        """Factory creates BLOCKED result with reason."""
        event = create_remediate_result_event(
            run_id="test_run",
            ts_ms=1600,
            mode="dry_run",
            action="cancel_all",
            symbol="BTCUSDT",
            client_order_id="manual_order_123",
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
            status="BLOCKED",
            block_reason="no_grinder_prefix",
        )

        assert event.status == "BLOCKED"
        assert event.block_reason == "no_grinder_prefix"

    def test_create_remediate_result_event_failed(self) -> None:
        """Factory creates FAILED result with error."""
        event = create_remediate_result_event(
            run_id="test_run",
            ts_ms=1600,
            mode="live",
            action="cancel_all",
            symbol="BTCUSDT",
            client_order_id="grinder_default_BTCUSDT_1_1000_1",
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
            status="FAILED",
            error="Connection timeout",
        )

        assert event.status == "FAILED"
        assert event.details["error"] == "Connection timeout"


# =============================================================================
# Determinism Tests
# =============================================================================


class TestDeterminism:
    """Tests for deterministic behavior."""

    def test_same_inputs_same_output(self, tmp_path: Path) -> None:
        """Same inputs produce identical JSONL output."""
        audit_path1 = tmp_path / "audit1.jsonl"
        audit_path2 = tmp_path / "audit2.jsonl"

        # Fixed clock and run_id for determinism
        fixed_clock = lambda: 1704067200000  # noqa: E731

        events = [
            AuditEvent(
                ts_ms=1000,
                event_type=AuditEventType.RECONCILE_RUN,
                run_id="run_1",
                mode="dry_run",
                action="cancel_all",
                symbols=("BTCUSDT",),
                mismatch_counts={"ORDER_EXISTS_UNEXPECTED": 1},
            ),
            AuditEvent(
                ts_ms=2000,
                event_type=AuditEventType.REMEDIATE_RESULT,
                run_id="run_1",
                status="PLANNED",
            ),
        ]

        # Write to two separate files
        for path in [audit_path1, audit_path2]:
            config = AuditConfig(enabled=True, path=str(path))
            with AuditWriter(config, _clock=fixed_clock) as writer:
                for event in events:
                    writer.write(event)

        # Compare outputs
        content1 = audit_path1.read_text()
        content2 = audit_path2.read_text()
        assert content1 == content2

    def test_json_key_order_stable(self) -> None:
        """JSON keys are always in sorted order."""
        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
            mode="dry_run",
            action="none",
            status="COMPLETED",
            symbols=("ETHUSDT", "BTCUSDT"),  # unsorted
        )

        line = event.to_json_line()
        data = json.loads(line)

        # Keys should be sorted
        keys = list(data.keys())
        assert keys == sorted(keys)


# =============================================================================
# Redaction Tests
# =============================================================================


class TestRedaction:
    """Tests for secret redaction."""

    def test_all_redacted_fields_covered(self) -> None:
        """All expected secrets are in REDACTED_FIELDS."""
        expected = {
            "api_key",
            "api_secret",
            "secret",
            "password",
            "token",
            "signature",
            "x-mbx-apikey",
            "authorization",
        }
        assert expected == REDACTED_FIELDS

    def test_partial_match_redacted(self, tmp_path: Path) -> None:
        """Fields containing secret names are redacted."""
        audit_path = tmp_path / "audit.jsonl"
        config = AuditConfig(enabled=True, path=str(audit_path), redact=True)
        writer = AuditWriter(config)

        event = AuditEvent(
            ts_ms=1000,
            event_type=AuditEventType.RECONCILE_RUN,
            run_id="test",
            details={
                "my_api_key_value": "should_be_redacted",
                "user_password_hash": "should_be_redacted",
                "normal": "visible",
            },
        )
        writer.write(event)
        writer.close()

        data = json.loads(audit_path.read_text().strip())
        assert data["details"]["my_api_key_value"] == "[REDACTED]"
        assert data["details"]["user_password_hash"] == "[REDACTED]"
        assert data["details"]["normal"] == "visible"


# =============================================================================
# Integration with Runner Tests
# =============================================================================


class TestRunnerIntegration:
    """Tests for AuditWriter integration with ReconcileRunner."""

    def test_runner_writes_audit_event(self, tmp_path: Path) -> None:
        """ReconcileRunner writes audit event when writer provided."""
        # Setup mocks
        mock_engine = MagicMock()
        mock_engine.reconcile.return_value = []

        mock_port = MagicMock()
        mock_observed = MagicMock()

        config = ReconcileConfig(action=RemediationAction.NONE, dry_run=True)
        executor = RemediationExecutor(
            config=config,
            port=mock_port,
            armed=False,
            symbol_whitelist=[],
        )

        # Setup audit writer
        audit_path = tmp_path / "audit.jsonl"
        audit_config = AuditConfig(enabled=True, path=str(audit_path))
        audit_writer = AuditWriter(audit_config, _clock=lambda: 1704067200000)

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=executor,
            observed=mock_observed,
            audit_writer=audit_writer,
            _clock=lambda: 1704067200000,
        )

        # Run reconciliation
        runner.run()
        audit_writer.close()

        # Verify audit event written
        assert audit_path.exists()
        data = json.loads(audit_path.read_text().strip())

        assert data["event_type"] == "RECONCILE_RUN"
        assert data["mode"] == "dry_run"
        assert data["action"] == "none"

    def test_runner_without_audit_works(self) -> None:
        """ReconcileRunner works without audit writer."""
        mock_engine = MagicMock()
        mock_engine.reconcile.return_value = []

        mock_port = MagicMock()
        mock_observed = MagicMock()

        config = ReconcileConfig(action=RemediationAction.NONE)
        executor = RemediationExecutor(
            config=config,
            port=mock_port,
            armed=False,
            symbol_whitelist=[],
        )

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=executor,
            observed=mock_observed,
            audit_writer=None,  # No audit
        )

        # Should not raise
        report = runner.run()
        assert report.mismatches_detected == 0
