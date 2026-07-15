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

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator
import re

import can
import numpy as np

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
    supports_raw_frame_arrays: bool = True

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

    def iter_decoded_channel_arrays(
        self,
        channel_config,
        progress=None,
        metadata_ready=None,
        raw_frame_batch=None,
    ):
        """Decode signals and expose raw CAN records through bulk arrays."""
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
        raw_executor = None
        raw_future = None
        self.raw_trace_error = ""
        try:
            source = asammdf.MDF(str(self._path), use_display_names=False)
            if raw_frame_batch is not None:
                # Raw CAN arrays are independent from the extracted decoded
                # MDF. Read them through a second lazy MDF handle so payload
                # materialisation overlaps native DBC extraction instead of
                # becoming another sequential loading phase.
                raw_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="canscope-mf4-trace",
                )
                raw_future = raw_executor.submit(
                    self._load_raw_frame_arrays,
                    asammdf,
                    raw_frame_batch,
                )
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

            # The hierarchy is available from decoded MDF metadata before any
            # sample arrays are selected. Hand it to the GUI immediately so
            # signal discovery tracks native asammdf extraction time.
            metadata_by_key = {}
            metadata_rows = []
            for group_idx, group in enumerate(extracted.groups):
                for ch_idx, decoded_channel in enumerate(group.channels):
                    signal_name = (
                        getattr(decoded_channel, "name", None) or f"Ch{ch_idx}"
                    )
                    if getattr(decoded_channel, "channel_type", -1) == 1:
                        continue
                    if signal_name.lower() in ("time", "t", "timestamps"):
                        continue
                    unit = str(getattr(decoded_channel, "unit", "") or "")
                    channel, message_name, message_id = self._decoded_group_metadata(
                        extracted, group_idx, signal_name, channel_config
                    )
                    meta = (
                        channel,
                        message_name,
                        message_id,
                        signal_name,
                        unit,
                    )
                    metadata_by_key[(group_idx, signal_name)] = meta
                    metadata_rows.append(meta)

            if metadata_ready is not None:
                metadata_ready(metadata_rows)

            for group_idx, old_meta, ts_arr, num_arr, disp_list in \
                    MDFReader._iter_arrays(
                        extracted,
                        include_group_index=True,
                        batch_all_groups=True,
                    ):
                signal_name = old_meta[1]
                unit = old_meta[2]
                meta = metadata_by_key.get((group_idx, signal_name))
                if meta is None:
                    channel, message_name, message_id = self._decoded_group_metadata(
                        extracted, group_idx, signal_name, channel_config
                    )
                else:
                    channel, message_name, message_id, _name, metadata_unit = meta
                    if not unit:
                        unit = metadata_unit
                yield (
                    (channel, message_name, message_id, signal_name, unit),
                    ts_arr,
                    num_arr,
                    disp_list,
                )

            if raw_future is not None:
                try:
                    raw_future.result()
                except Exception as exc:
                    # A trace-only failure must not discard a successful native
                    # signal decode or force the much slower compatibility path.
                    self.raw_trace_error = str(exc)
        except MDFCANReadError:
            raise
        except Exception as exc:
            raise MDFCANReadError(
                f"asammdf bus extraction failed for '{self._path}': {exc}"
            ) from exc
        finally:
            if raw_executor is not None:
                raw_executor.shutdown(wait=True, cancel_futures=False)
            for mdf in (extracted, source):
                if mdf is not None:
                    try:
                        mdf.close()
                    except Exception:
                        pass

    def _load_raw_frame_arrays(self, asammdf, callback) -> int:
        raw_source = None
        try:
            raw_source = asammdf.MDF(str(self._path), use_display_names=False)
            return self._emit_raw_frame_arrays(raw_source, callback)
        finally:
            if raw_source is not None:
                try:
                    raw_source.close()
                except Exception:
                    pass

    @staticmethod
    def _emit_raw_frame_arrays(source, callback) -> int:
        """Read each MDF ``CAN_DataFrame`` group as one structured array."""
        total = 0
        for group_idx, group in enumerate(source.groups):
            parent_idx = None
            for channel_idx, channel in enumerate(group.channels):
                if (getattr(channel, "name", "") or "") == "CAN_DataFrame":
                    parent_idx = channel_idx
                    break
            if parent_idx is None:
                continue

            signal = source.get(
                group=group_idx,
                index=parent_idx,
                raw=True,
            )
            samples = np.asarray(signal.samples)
            field_names = samples.dtype.names or ()

            def field(suffix, required=True):
                exact = f"CAN_DataFrame.{suffix}"
                name = exact if exact in field_names else next(
                    (item for item in field_names if item.endswith(f".{suffix}")),
                    None,
                )
                if name is None:
                    if required:
                        raise MDFCANReadError(
                            f"Raw CAN channel '{exact}' is missing from group {group_idx}."
                        )
                    return None
                return samples[name]

            timestamps = np.asarray(signal.timestamps, dtype=np.float64)
            channels = field("BusChannel")
            arb_ids = field("ID")
            data_rows = field("DataBytes")
            data_lengths = field("DataLength", required=False)
            if data_lengths is None:
                data_lengths = field("DLC")
            directions = field("Dir", required=False)
            if directions is None:
                directions = np.full(len(samples), 2, dtype=np.uint8)
            ide = field("IDE", required=False)
            if ide is None:
                ide = np.asarray(arb_ids, dtype=np.uint32) > 0x7FF
            edl = field("EDL", required=False)
            if edl is None:
                edl = np.zeros(len(samples), dtype=np.uint8)
            flags = (
                (np.asarray(ide, dtype=np.uint8) & np.uint8(1))
                | ((np.asarray(edl, dtype=np.uint8) & np.uint8(1)) << np.uint8(1))
            )

            callback(
                timestamps,
                channels,
                arb_ids,
                data_lengths,
                directions,
                flags,
                data_rows,
            )
            total += len(timestamps)
        return total

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
