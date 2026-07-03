"""Tests for core/readers/csv_reader.py — narrow and wide format reading."""
from __future__ import annotations

import pytest

from core.readers.csv_reader import CSVSignalReader, CSVReadError


# ── Narrow format ─────────────────────────────────────────────────────────

def test_narrow_yields_seven_samples(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    assert len(list(reader)) == 7


def test_narrow_signal_names(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    names = {s.signal_name for s in reader}
    assert "EngSpeed" in names
    assert "Throttle" in names
    assert "Gear" in names


def test_narrow_channel_is_int(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    for s in reader:
        assert s.channel == 1


def test_narrow_timestamps_are_floats(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    for s in reader:
        assert isinstance(s.timestamp, float)


def test_narrow_eng_speed_value(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    eng = [s for s in reader if s.signal_name == "EngSpeed"]
    assert eng[0].numeric_value == pytest.approx(600.0)


def test_narrow_unit(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    eng = [s for s in reader if s.signal_name == "EngSpeed"]
    assert eng[0].unit == "rpm"


def test_narrow_format_detected(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    list(reader)  # trigger detection
    assert any("NARROW" in m for m in reader.load_messages)


def test_narrow_message_name(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    eng = [s for s in reader if s.signal_name == "EngSpeed"]
    assert eng[0].message_name == "EngineControl"


# ── Wide format ───────────────────────────────────────────────────────────

def test_wide_yields_six_samples(wide_csv_path):
    # 2 signals × 3 rows = 6 samples
    reader = CSVSignalReader(wide_csv_path)
    assert len(list(reader)) == 6


def test_wide_signal_names(wide_csv_path):
    reader = CSVSignalReader(wide_csv_path)
    names = {s.signal_name for s in reader}
    assert "EngSpeed" in names
    assert "Throttle" in names


def test_wide_units_parsed(wide_csv_path):
    reader = CSVSignalReader(wide_csv_path)
    samples = list(reader)
    eng = [s for s in samples if s.signal_name == "EngSpeed"]
    assert eng[0].unit == "rpm"


def test_wide_format_detected(wide_csv_path):
    reader = CSVSignalReader(wide_csv_path)
    list(reader)
    assert any("WIDE" in m for m in reader.load_messages)


def test_wide_message_name_is_csv(wide_csv_path):
    reader = CSVSignalReader(wide_csv_path)
    samples = list(reader)
    assert all(s.message_name == "CSV" for s in samples)


def test_wide_channel_is_none(wide_csv_path):
    reader = CSVSignalReader(wide_csv_path)
    samples = list(reader)
    assert all(s.channel is None for s in samples)


# ── Error handling ────────────────────────────────────────────────────────

def test_missing_file_raises():
    with pytest.raises(CSVReadError):
        CSVSignalReader("/nonexistent/path/file.csv")


def test_source_description_contains_filename(narrow_csv_path):
    reader = CSVSignalReader(narrow_csv_path)
    assert "sample_narrow.csv" in reader.source_description
