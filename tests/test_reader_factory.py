"""Tests for core/readers/__init__.py — reader_factory and dbc_required_for."""
from __future__ import annotations

import pytest

from core.readers import reader_factory, dbc_required_for, UnsupportedFormatError
from core.readers.csv_reader import CSVSignalReader


# ── Missing-DBC errors ─────────────────────────────────────────────────────

def test_blf_without_dbc_raises(blf_path):
    with pytest.raises(ValueError, match="DBC or ARXML"):
        reader_factory(str(blf_path), dbc_path=None)


def test_asc_without_dbc_raises(asc_path):
    with pytest.raises(ValueError, match="DBC or ARXML"):
        reader_factory(str(asc_path), dbc_path=None)


# ── Unsupported extension ─────────────────────────────────────────────────

def test_unsupported_extension_raises(tmp_path):
    p = tmp_path / "file.xyz"
    p.write_bytes(b"")
    with pytest.raises(UnsupportedFormatError):
        reader_factory(str(p))


# ── CSV needs no DBC ──────────────────────────────────────────────────────

def test_csv_returns_csv_reader(narrow_csv_path):
    reader = reader_factory(str(narrow_csv_path))
    assert isinstance(reader, CSVSignalReader)


def test_csv_no_dbc_required(narrow_csv_path):
    assert dbc_required_for(str(narrow_csv_path)) is False


# ── BLF / ASC return correct reader types ─────────────────────────────────

def test_blf_returns_blf_reader(blf_path, sample_dbc_path):
    from core.readers.blf_can_reader import BLFCANReader
    reader = reader_factory(str(blf_path), dbc_path=str(sample_dbc_path))
    assert isinstance(reader, BLFCANReader)


def test_asc_returns_asc_reader(asc_path, sample_dbc_path):
    from core.readers.asc_can_reader import ASCCANReader
    reader = reader_factory(str(asc_path), dbc_path=str(sample_dbc_path))
    assert isinstance(reader, ASCCANReader)


# ── dbc_required_for ─────────────────────────────────────────────────────

def test_dbc_required_for_blf(blf_path):
    assert dbc_required_for(str(blf_path)) is True


def test_dbc_required_for_asc(asc_path):
    assert dbc_required_for(str(asc_path)) is True


def test_dbc_required_for_csv_false(narrow_csv_path):
    assert dbc_required_for(str(narrow_csv_path)) is False
