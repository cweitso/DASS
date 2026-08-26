import threading
import time

import pytest
from sqlalchemy import create_engine, text

# Without this the -m integration run silently skipped the whole file.
pytestmark = pytest.mark.integration


def test_scheduler_leader_election(main_engine):
    """
    A session-level advisory lock is released when its connection closes.

    That is what makes scheduler leadership fail over: a crashed leader drops its
    connection and the standby's next attempt succeeds.
    """
    LOCK_KEY = 999999
    results = {}

    from sqlalchemy.pool import NullPool
    # Scheduler 1 wins the lock first and becomes leader.
    def run_scheduler_1():
        # NullPool so the connection really closes on exit, simulating a crash.
        engine = create_engine(main_engine.url, poolclass=NullPool)
        with engine.connect() as conn:
            is_leader = conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            ).scalar()

            results["s1_is_leader"] = is_leader

            if is_leader:
                # Hold the lock long enough for scheduler 2 to try and fail.
                time.sleep(2)
        # Leaving the block closes the connection, which releases the lock.

    # Scheduler 2 starts as standby and takes over once the leader disappears.
    def run_scheduler_2():
        engine = create_engine(main_engine.url, poolclass=NullPool)
        with engine.connect() as conn:
            # First attempt, while scheduler 1 still holds the lock.
            first_try = conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            ).scalar()
            results["s2_first_try"] = first_try

            # Wait out scheduler 1's 2s hold plus a margin.
            time.sleep(3)

            # Second attempt, after scheduler 1 has disconnected.
            second_try = conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            ).scalar()
            results["s2_second_try"] = second_try

    # Run both schedulers concurrently.
    t1 = threading.Thread(target=run_scheduler_1)
    t2 = threading.Thread(target=run_scheduler_2)

    # Start scheduler 1 first so it is guaranteed the lock.
    t1.start()
    time.sleep(0.5)

    # Then start scheduler 2.
    t2.start()

    # Wait for both.
    t1.join()
    t2.join()

    # Scheduler 2 must fail first and succeed after the leader is gone.
    assert results["s1_is_leader"] is True, "S1 should get LOCK_KEY"
    assert results["s2_first_try"] is False, "S2 should not get LOCK_KEY"
    assert (
        results["s2_second_try"] is True
    ), "S1 is disconnected, S2 should get LOCK_KEY"
