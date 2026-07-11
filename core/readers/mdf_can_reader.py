"""
MDFCANReader — reads ASAM MDF4 bus logging files (.mf4 / .mdf) that store
raw CAN frames using the ``CAN_DataFrame.*`` channel group structure.

python-can's ``MF4Reader`` wraps asammdf internally and yields
``can.Message`` objects — the exact same interface as ``BLFReader`` and
``ASCReader``.  This means the entire existing DBC decode pipeline
(DBCDecoder, LoadWorker, RawFrameStore, DBC Manager) works without any
changes.

Requires both ``asammdf>=7.0`` and the MF4 support in ``python-can>=4.6``.

Usage in ``reader_factory``::

    if MDFReader.is_bus_logging(path):          # fast probe, < 50 ms
        return MDFCANReader(path, DBCDecoder(dbc_path))
    else:
        return MDFReader(path)                   # pre-decoded path

``BusChannel`` field maps to CAN channel numbers directly — 1-indexed
as stored in the MDF file (Vector / ASAM convention is 1-based).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator
import re

import can

from core.models import RawFrame, DecodedSignalSample
from core.dbc_decoder import DBCDecoder
from core.readers.mdf_reader import MDFReader


class MDFCANReadError(RuntimeError):
    pass


class MDFCANReader:
    """
    Reads an ASAM MDF4 bus logging file and decodes signals with a
    :class:`DBCDecoder`.

    ``python-can`` ``MF4Reader`` yields ``can.Message`` objects, so the
    frame-to-RawFrame conversion is identical to :class:`ASCCANReader`.

    Attributes
    ----------
    source_description : str
    has_raw_frames : bool  — always ``True``
    """

    has_raw_frames: bool = True

    def __init__(self, mdf_path: str | Path, decoder: DBCDecoder | str | Path) -> None:
        self._path = Path(mdf_path)
        if isinstance(decoder, DBCDecoder) or (
            hasattr(decoder, "decode_frame") and hasattr(decoder, "dbc_path")
        ):
            self._decoder: DBCDecoder | None = decoder
            self._dbc_path = decoder.dbc_path
            decoder_messages = list(decoder.load_messages)
        else:
            self._decoder = None
            self._dbc_path = Path(decoder)
            decoder_messages = []
        fmt = "MF4" if self._path.suffix.lower() == ".mf4" else "MDF"
        self.source_description = (
            f"{fmt} bus log + DBC  ({self._path.name} / {self._dbc_path.name})"
        )
        self.load_messages: list[str] = [
            f"MDF bus logging: opening {fmt} file (raw CAN frames)…",
            f"DBC: {self._dbc_path.name}",
            "Fast path: asammdf native one-pass bus extraction.",
        ] + decoder_messages

    # ── Protocol iterator ─────────────────────────────────────────────────

    def __iter__(self) -> Iterator[DecodedSignalSample]:
        for _frame, samples in self.iter_with_frames():
            yield from samples

    def _messages(self):
        """Yield python-can messages while centralising MF4 error handling."""
        if not self._path.exists():
            raise MDFCANReadError(f"MDF file not found: {self._path}")

        try:
            reader = can.MF4Reader(str(self._path))
        except AttributeError:
            raise MDFCANReadError(
                "python-can's MF4Reader is not available.\n"
                "Ensure python-can >= 4.6 and asammdf >= 7.0 are installed."
            )
        except Exception as exc:
            raise MDFCANReadError(
                f"Failed to open MDF bus log '{self._path}': {exc}"
            ) from exc

        try:
            with reader:
                for msg in reader:
                    if hasattr(msg, "arbitration_id"):
                        yield msg
        except MDFCANReadError:
            raise
        except Exception as exc:
            raise MDFCANReadError(
                f"Error reading MDF bus log '{self._path}': {exc}"
            ) from exc

    def iter_raw_tuples(self):
        """Yield allocation-light tuples for LoadWorker's vectorized path.

        Decoding is deliberately omitted here.  LoadWorker groups these raw
        frames by channel and arbitration ID, then decodes each group with
        NumPy instead of calling cantools for every individual MF4 frame.
        """
        for msg in self._messages():
            data = bytes(msg.data or b"")
            raw_ch = getattr(msg, "channel", None)
            channel = int(raw_ch) if isinstance(raw_ch, (int, float)) else 255
            is_rx = getattr(msg, "is_rx", None)
            direction = 0 if is_rx is True else 1 if is_rx is False else 2
            yield (
                float(msg.timestamp),
                channel,
                int(msg.arbitration_id),
                int(getattr(msg, "dlc", len(data))),
                direction,
                bool(getattr(msg, "is_extended_id", False)),
                bool(getattr(msg, "is_fd", False)),
                data,
            )

    def iter_frames_only(self) -> Iterator[RawFrame]:
        """Yield raw frames without per-frame DBC decoding."""
        for ts, ch, arb_id, dlc, direction, is_ext, is_fd, data in self.iter_raw_tuples():
            yield RawFrame(
                timestamp=ts,
                channel=None if ch == 255 else ch,
                arbitration_id=arb_id,
                is_extended_id=is_ext,
                is_fd=is_fd,
                dlc=dlc,
                data=data,
                direction=("Rx", "Tx", "Unknown")[direction],
            )

    def iter_decoded_channel_arrays(self, channel_config, progress=None):
        """Decode the complete MF4 bus log with asammdf in one native pass."""
        try:
            import asammdf
        except ImportError as exc:
            raise MDFCANReadError("asammdf is required for MF4 bus decoding.") from exc

        databases = []
        if channel_config is not None:
            databases = [
                (path, int(channel))
                for channel, path in channel_config.channels.items()
            ]
        if not databases:
            databases = [(str(self._dbc_path), 0)]

        source = extracted = None
        try:
            source = asammdf.MDF(str(self._path), use_display_names=False)
            # Expand CANScope's "All Channels" fallback to the concrete bus
            # channels discovered by asammdf.  This prevents a channel-specific
            # database and the fallback database from both decoding the same bus.
            if channel_config is not None:
                try:
                    can_map = source._mdf.bus_logging_map.get("CAN", {})
                    concrete = []
                    for channel in can_map:
                        path = channel_config.dbc_path_for(int(channel))
                        if path:
                            concrete.append((path, int(channel)))
                    if concrete:
                        databases = list(dict.fromkeys(concrete))
                except Exception:
                    pass
            extracted = source.extract_bus_logging(
                database_files={"CAN": databases},
                ignore_value2text_conversion=False,
                progress=progress,
            )

            for group_idx, old_meta, ts_arr, num_arr, disp_list in \
                    MDFReader._iter_arrays(extracted, include_group_index=True):
                signal_name = old_meta[1]
                unit = old_meta[2]
                channel, message_name, message_id = self._decoded_group_metadata(
                    extracted, group_idx, signal_name, channel_config
                )
                yield (
                    (channel, message_name, message_id, signal_name, unit),
                    ts_arr,
                    num_arr,
                    disp_list,
                )
        except MDFCANReadError:
            raise
        except Exception as exc:
            raise MDFCANReadError(
                f"asammdf bus extraction failed for '{self._path}': {exc}"
            ) from exc
        finally:
            for mdf in (extracted, source):
                if mdf is not None:
                    try:
                        mdf.close()
                    except Exception:
                        pass

    @staticmethod
    def _decoded_group_metadata(extracted, group_idx, signal_name, channel_config):
        """Recover CAN channel/message identity from asammdf group metadata."""
        group = extracted.groups[group_idx]
        channel_group = getattr(group, "channel_group", None)
        source = getattr(channel_group, "acq_source", None)
        source_path = str(getattr(source, "path", "") or "")
        acq_name = str(getattr(channel_group, "acq_name", "") or "")
        text = f"{source_path} {acq_name}"

        channel_match = re.search(r"\bCAN(\d+)\b", text, re.IGNORECASE)
        id_match = re.search(r"\bID=0x([0-9A-F]+)", text, re.IGNORECASE)
        channel = int(channel_match.group(1)) if channel_match else None
        message_id = int(id_match.group(1), 16) if id_match else 0

        message_name = ""
        try:
            locations = extracted.channels_db.get(signal_name, ())
            for gi, ci in locations:
                if gi != group_idx:
                    continue
                display_names = getattr(group.channels[ci], "display_names", {}) or {}
                for display_name in display_names:
                    match = re.match(r"CAN\d+\.(.+)\.[^.]+$", display_name)
                    if match:
                        message_name = match.group(1)
                        break
                if message_name:
                    break
        except Exception:
            pass

        if not message_name and channel_config is not None and message_id:
            try:
                decoder = channel_config.decoder_for(channel)
                if decoder is not None:
                    from core.vectorized_decoder import VectorizedDBC
                    candidates = VectorizedDBC(decoder).get_candidates(
                        message_id, is_extended=(message_id > 0x7FF)
                    )
                    if candidates:
                        message_name = candidates[0].name
            except Exception:
                pass

        if not message_name:
            message_name = f"CAN_DataFrame_{message_id:X}"
        return channel, message_name, message_id

    # ── Extended iterator (used by LoadWorker) ────────────────────────────

    def iter_with_frames(self) -> Iterator[tuple[RawFrame, list[DecodedSignalSample]]]:
        """
        Yield (RawFrame, decoded_samples) pairs — identical contract to
        BLFCANReader and ASCCANReader.

        python-can's MF4Reader handles the asammdf call internally.
        ``BusChannel`` is already 1-indexed in the MDF4 bus logging standard
        (unlike BLF/ASC which use 0-indexed and need +1).
        """
        try:
            for msg in self._messages():
                data = bytes(msg.data or b"")
                is_rx = getattr(msg, "is_rx", None)
                direction = (
                    "Rx"      if is_rx is True  else
                    "Tx"      if is_rx is False else
                    "Unknown"
                )

                # MDF bus logging BusChannel is already 1-indexed
                raw_ch = getattr(msg, "channel", None)
                channel = int(raw_ch) if isinstance(raw_ch, (int, float)) else None

                frame = RawFrame(
                    timestamp      = float(msg.timestamp),
                    channel        = channel,
                    arbitration_id = int(msg.arbitration_id),
                    is_extended_id = bool(getattr(msg, "is_extended_id", False)),
                    is_fd          = bool(getattr(msg, "is_fd", False)),
                    dlc            = int(getattr(msg, "dlc", len(data))),
                    data           = data,
                    direction      = direction,
                )
                samples = self.decoder.decode_frame(frame)
                yield frame, samples

        except MDFCANReadError:
            raise
        except Exception as exc:
            raise MDFCANReadError(
                f"Error reading MDF bus log '{self._path}': {exc}"
            ) from exc

    @property
    def decoder(self) -> DBCDecoder:
        if self._decoder is None:
            self._decoder = DBCDecoder(self._dbc_path)
        return self._decoder
