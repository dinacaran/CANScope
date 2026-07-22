"""
Shared pytest fixtures for the CANScope test suite.

Binary fixtures (sample.blf, sample.asc) are generated on first run
by tests/fixtures/_generate.py and are intentionally not committed to git.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# ── Strict fixture mode ────────────────────────────────────────────────────
# Binary fixtures are generated on demand and not committed, so a generation
# failure would normally skip every BLF/ASC test and leave the run green —
# silently disabling coverage of the loading/decoding pipeline. CI sets
# CANSCOPE_STRICT_FIXTURES=1 to turn those skips into hard failures.
STRICT_FIXTURES = bool(os.environ.get("CANSCOPE_STRICT_FIXTURES"))


def skip_or_fail(reason: str) -> None:
    """Skip locally, fail under CANSCOPE_STRICT_FIXTURES. Never returns."""
    if STRICT_FIXTURES:
        pytest.fail(f"{reason} (CANSCOPE_STRICT_FIXTURES is set)")
    pytest.skip(reason)


# Keep diagnostic telemetry out of the repo's logs/ during test runs.
@pytest.fixture(autouse=True)
def _no_diag_telemetry(monkeypatch):
    monkeypatch.setenv("CANSCOPE_DIAG_TELEMETRY", "0")

# ── Payload constants (match sample.dbc signal layout) ────────────────────
# EngineControl 0x100: EngSpeed raw=2400 (0x0960 LE) → 1200.0 rpm; Throttle raw=100 → 50.0 %
ENG_PAYLOAD  = bytes([0x60, 0x09, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00])
# GearStatus 0x200: Gear raw=4 → Drive
GEAR_PAYLOAD = bytes([0x04, 0x00, 0x00, 0x00])
# DiagRequest 0x300: 8 zero bytes, no signals
DIAG_PAYLOAD = bytes(8)


# ── Path fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def sample_dbc_path() -> Path:
    return FIXTURES_DIR / "sample.dbc"


@pytest.fixture(scope="session")
def motor_control_yaml_path() -> Path:
    return FIXTURES_DIR / "motor_control_test.yaml"


@pytest.fixture(scope="session")
def legacy_v1_path() -> Path:
    return FIXTURES_DIR / "legacy_v1.canscope_ch"


@pytest.fixture(scope="session")
def narrow_csv_path() -> Path:
    return FIXTURES_DIR / "sample_narrow.csv"


@pytest.fixture(scope="session")
def wide_csv_path() -> Path:
    return FIXTURES_DIR / "sample_wide.csv"


@pytest.fixture()
def raw_can_csv_path(tmp_path) -> Path:
    path = tmp_path / "sample_can.csv"
    path.write_text(
        "TimestampEpoch;BusChannel;ID;IDE;DLC;DataLength;Dir;EDL;BRS;ESI;RTR;DataBytes\n"
        "1660503551.100000;1;100;0;8;8;0;0;0;0;0;6009640000000000\n"
        "1660503551.200000;1;200;0;4;4;1;0;0;0;0;04000000\n"
        "1660503551.300000;2;18FF50E5;1;9;12;0;1;0;0;0;000102030405060708090A0B\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="session")
def blf_path() -> Path:
    path = FIXTURES_DIR / "sample.blf"
    if not path.exists():
        try:
            subprocess.check_call(
                [sys.executable, str(FIXTURES_DIR / "_generate.py")],
                timeout=30,
            )
        except Exception as exc:
            skip_or_fail(f"Could not generate sample.blf: {exc}")
    if not path.exists():
        skip_or_fail("sample.blf not found — run tests/fixtures/_generate.py")
    return path


@pytest.fixture(scope="session")
def asc_path() -> Path:
    path = FIXTURES_DIR / "sample.asc"
    if not path.exists():
        try:
            subprocess.check_call(
                [sys.executable, str(FIXTURES_DIR / "_generate.py")],
                timeout=30,
            )
        except Exception as exc:
            skip_or_fail(f"Could not generate sample.asc: {exc}")
    if not path.exists():
        skip_or_fail("sample.asc not found — run tests/fixtures/_generate.py")
    return path


# ── Decoder fixture (function-scoped so stats are clean per test) ──────────

@pytest.fixture()
def decoder(sample_dbc_path):
    from core.dbc_decoder import DBCDecoder
    return DBCDecoder(str(sample_dbc_path))


# ── Raw frame fixtures ─────────────────────────────────────────────────────

@pytest.fixture()
def frame_engine():
    from core.models import RawFrame
    return RawFrame(
        timestamp=0.001,
        channel=1,
        arbitration_id=0x100,
        is_extended_id=False,
        is_fd=False,
        dlc=8,
        data=ENG_PAYLOAD,
        direction="Rx",
    )


@pytest.fixture()
def frame_gear():
    from core.models import RawFrame
    return RawFrame(
        timestamp=0.002,
        channel=1,
        arbitration_id=0x200,
        is_extended_id=False,
        is_fd=False,
        dlc=4,
        data=GEAR_PAYLOAD,
        direction="Rx",
    )


@pytest.fixture()
def frame_diag():
    from core.models import RawFrame
    return RawFrame(
        timestamp=0.003,
        channel=1,
        arbitration_id=0x300,
        is_extended_id=False,
        is_fd=False,
        dlc=8,
        data=DIAG_PAYLOAD,
        direction="Rx",
    )


# ── SignalStore fixture ────────────────────────────────────────────────────

@pytest.fixture()
def signal_store():
    from core.signal_store import SignalStore
    return SignalStore()


# ── Helper: populate a SignalStore with synthetic EngSpeed data ────────────

def _ts_for(vals: list[float]) -> np.ndarray:
    n = len(vals)
    return np.linspace(0.001, 0.001 * n, n)


def make_store_with_signals(
    eng_speed_vals: list[float],
    throttle_vals: list[float] | None = None,
    gear_vals: list[float] | None = None,
):
    """Return a SignalStore loaded with synthetic data matching sample.dbc signals."""
    from core.signal_store import SignalStore

    store = SignalStore()

    store.add_series_bulk(
        channel=1,
        message_name="EngineControl",
        message_id=0x100,
        signal_name="EngSpeed",
        unit="rpm",
        timestamps=_ts_for(eng_speed_vals),
        values=np.array(eng_speed_vals, dtype=np.float64),
        raw_values=[],
        has_labels=False,
    )

    if throttle_vals is not None:
        store.add_series_bulk(
            channel=1,
            message_name="EngineControl",
            message_id=0x100,
            signal_name="Throttle",
            unit="%",
            timestamps=_ts_for(throttle_vals),
            values=np.array(throttle_vals, dtype=np.float64),
            raw_values=[],
            has_labels=False,
        )

    if gear_vals is not None:
        store.add_series_bulk(
            channel=1,
            message_name="GearStatus",
            message_id=0x200,
            signal_name="Gear",
            unit="",
            timestamps=_ts_for(gear_vals),
            values=np.array(gear_vals, dtype=np.float64),
            raw_values=[],
            has_labels=False,
        )

    return store


def make_store_with_named_signals(
    signals: dict[str, tuple[np.ndarray, np.ndarray]],
    *,
    channel: int = 1,
):
    """Return a SignalStore holding arbitrary named signals with explicit,
    possibly *offset* timebases.

    ``signals`` maps signal name -> (timestamps, values).  Each signal gets its
    own synthetic message so store keys stay distinct.  Used by the ZOH /
    episode / knowledge-enrichment tests which need EngFault, Active_DTC_ID,
    OilPressure, EngSpeed on independent timelines.
    """
    from core.signal_store import SignalStore

    store = SignalStore()
    for i, (name, (ts, vals)) in enumerate(signals.items()):
        store.add_series_bulk(
            channel=channel,
            message_name=f"{name}Msg",
            message_id=0x400 + i,
            signal_name=name,
            unit="",
            timestamps=np.asarray(ts, dtype=np.float64),
            values=np.asarray(vals, dtype=np.float64),
            raw_values=[],
            has_labels=False,
        )
    return store


# ── Minimal DomainConfig for unit tests ───────────────────────────────────

def make_test_domain(name: str = "TestDomain", context_window_s: float = 2.0):
    """Create a minimal DomainConfig with empty signal_map for rule processor tests."""
    from core.diagnostics.config_loader import DomainConfig
    from pathlib import Path

    return DomainConfig(
        name=name,
        description="",
        signal_map={},
        rules=[],
        source_path=Path("test.yaml"),
        context_window_s=context_window_s,
    )
