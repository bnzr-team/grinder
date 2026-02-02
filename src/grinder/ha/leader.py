"""Redis-based leader election with lease lock.

This module implements a TTL-based lease lock for single-active coordination.

Safety guarantees:
- Only one instance can be ACTIVE at any time
- If lock is lost, instance immediately becomes STANDBY (fail-safe)
- Lock renewal period < TTL to prevent expiry during normal operation

Limitations (documented in DECISIONS.md):
- Single-host only (Redis is SPOF)
- No protection against host/VM failure
- Redis failure = all instances become STANDBY
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

import redis

from grinder.ha.role import HARole, get_ha_state, set_ha_state

logger = logging.getLogger(__name__)


def _get_int_env(key: str, default: int) -> int:
    """Get integer from environment variable."""
    value = os.environ.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass
class LeaderElectorConfig:
    """Configuration for leader election.

    Attributes:
        redis_url: Redis connection URL (env: GRINDER_REDIS_URL)
        lock_key: Key name for the distributed lock
        lock_ttl_ms: Lock TTL in milliseconds (env: GRINDER_HA_LOCK_TTL_MS, default: 10000)
        renew_interval_ms: How often to renew lock (env: GRINDER_HA_RENEW_INTERVAL_MS, default: 3000)
        instance_id: Unique ID for this instance (auto-generated if empty)
    """

    redis_url: str = field(
        default_factory=lambda: os.environ.get("GRINDER_REDIS_URL", "redis://localhost:6379/0")
    )
    lock_key: str = "grinder:leader:lock"
    lock_ttl_ms: int = field(default_factory=lambda: _get_int_env("GRINDER_HA_LOCK_TTL_MS", 10000))
    renew_interval_ms: int = field(
        default_factory=lambda: _get_int_env("GRINDER_HA_RENEW_INTERVAL_MS", 3000)
    )
    instance_id: str = field(default_factory=lambda: f"grinder-{uuid.uuid4().hex[:8]}")

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.lock_ttl_ms < 1000:
            msg = f"lock_ttl_ms ({self.lock_ttl_ms}) should be >= 1000ms for safety"
            raise ValueError(msg)
        if self.renew_interval_ms >= self.lock_ttl_ms:
            msg = f"renew_interval_ms ({self.renew_interval_ms}) must be < lock_ttl_ms ({self.lock_ttl_ms})"
            raise ValueError(msg)


class LeaderElector:
    """Redis-based leader election manager.

    Usage:
        config = LeaderElectorConfig()
        elector = LeaderElector(config)
        elector.start()  # Starts background renewal thread
        ...
        elector.stop()   # Stop and release lock

    The elector automatically updates HAState based on lock status.
    """

    def __init__(self, config: LeaderElectorConfig) -> None:
        self._config = config
        self._redis: redis.Redis[bytes] | None = None
        self._stop_event = threading.Event()
        self._renewal_thread: threading.Thread | None = None
        self._is_running = False

        # Set instance ID in state
        set_ha_state(instance_id=config.instance_id)

    def start(self) -> None:
        """Start the leader election process.

        Connects to Redis and starts the lock renewal thread.
        """
        if self._is_running:
            return

        self._redis = redis.Redis.from_url(
            self._config.redis_url,
            decode_responses=False,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )

        # Verify connection
        try:
            self._redis.ping()
            logger.info(
                "Connected to Redis",
                extra={
                    "redis_url": self._config.redis_url,
                    "instance_id": self._config.instance_id,
                },
            )
        except redis.ConnectionError as e:
            logger.error("Failed to connect to Redis: %s", e)
            set_ha_state(role=HARole.STANDBY, lock_failures=1)
            raise

        self._stop_event.clear()
        self._renewal_thread = threading.Thread(
            target=self._renewal_loop,
            name=f"leader-elector-{self._config.instance_id}",
            daemon=True,
        )
        self._renewal_thread.start()
        self._is_running = True

    def stop(self) -> None:
        """Stop the leader election and release the lock."""
        if not self._is_running:
            return

        self._stop_event.set()
        if self._renewal_thread:
            self._renewal_thread.join(timeout=5.0)

        # Release lock if we hold it
        self._release_lock()

        if self._redis:
            self._redis.close()
            self._redis = None

        set_ha_state(role=HARole.STANDBY)
        self._is_running = False
        logger.info("Leader elector stopped", extra={"instance_id": self._config.instance_id})

    def _renewal_loop(self) -> None:
        """Background thread that attempts to acquire/renew the lock."""
        interval_s = self._config.renew_interval_ms / 1000.0

        while not self._stop_event.is_set():
            try:
                self._attempt_lock()
            except Exception:
                logger.exception("Error in lock attempt")
                # On any error, become standby (fail-safe)
                self._become_standby()

            self._stop_event.wait(timeout=interval_s)

    def _attempt_lock(self) -> None:
        """Try to acquire or renew the lock."""
        if self._redis is None:
            return

        now_ms = int(time.time() * 1000)
        set_ha_state(last_lock_attempt_ms=now_ms)

        instance_id_bytes = self._config.instance_id.encode()
        lock_key = self._config.lock_key
        ttl_ms = self._config.lock_ttl_ms

        try:
            # Try to set lock with NX (only if not exists) and PX (expire in ms)
            # If we already hold it, we need to renew with SET XX
            current_holder = self._redis.get(lock_key)

            if current_holder == instance_id_bytes:
                # We hold the lock, renew it
                result = self._redis.set(lock_key, instance_id_bytes, px=ttl_ms, xx=True)
                if result:
                    self._become_active()
                else:
                    # Lock was deleted between GET and SET XX
                    self._become_standby()
            elif current_holder is None:
                # Lock is free, try to acquire
                result = self._redis.set(lock_key, instance_id_bytes, px=ttl_ms, nx=True)
                if result:
                    self._become_active()
                else:
                    # Someone else grabbed it
                    self._become_standby()
            else:
                # Someone else holds the lock
                holder_id = current_holder.decode() if current_holder else "unknown"
                set_ha_state(lock_holder=holder_id)
                self._become_standby()

        except redis.ConnectionError:
            logger.warning("Redis connection lost, becoming standby")
            self._become_standby()
            state = get_ha_state()
            set_ha_state(lock_failures=state.lock_failures + 1)

    def _become_active(self) -> None:
        """Transition to ACTIVE role."""
        current = get_ha_state()
        if current.role != HARole.ACTIVE:
            logger.info(
                "Became ACTIVE (acquired lock)",
                extra={"instance_id": self._config.instance_id},
            )
        set_ha_state(
            role=HARole.ACTIVE,
            lock_holder=self._config.instance_id,
            lock_failures=0,
        )

    def _become_standby(self) -> None:
        """Transition to STANDBY role (fail-safe)."""
        current = get_ha_state()
        if current.role == HARole.ACTIVE:
            logger.warning(
                "Lost lock, becoming STANDBY",
                extra={"instance_id": self._config.instance_id},
            )
        elif current.role == HARole.UNKNOWN:
            logger.info(
                "Starting as STANDBY",
                extra={"instance_id": self._config.instance_id},
            )
        set_ha_state(role=HARole.STANDBY)

    def _release_lock(self) -> None:
        """Release the lock if we hold it."""
        if self._redis is None:
            return

        try:
            # Only delete if we hold it (atomic check-and-delete via Lua)
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            self._redis.eval(lua_script, 1, self._config.lock_key, self._config.instance_id)
            logger.info("Released lock", extra={"instance_id": self._config.instance_id})
        except redis.ConnectionError:
            logger.warning("Could not release lock (Redis connection lost)")

    @property
    def is_active(self) -> bool:
        """Check if this instance is currently ACTIVE."""
        return get_ha_state().role == HARole.ACTIVE

    @property
    def is_running(self) -> bool:
        """Check if the elector is running."""
        return self._is_running
