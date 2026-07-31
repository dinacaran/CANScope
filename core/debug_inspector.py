"""Read-only forensic inspection for CANScope measurement load problems.

The normal measurement readers deliberately remain untouched.  This module
uses independent, bounded probes so a failure in python-can or asammdf still
leaves enough structural evidence to diagnose the file from plain text.

No report is written to disk.  Callers own the returned text.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Iterable, Mapping
import gc
import platform
import struct
import sys
import traceback
import zlib


def _collect_failed_mdf_open() -> None:
    """Finalise the wreckage of an ``asammdf.MDF`` constructor that raised.

    A constructor that fails part-way leaves behind an MDF4 we never get a
    handle on, and its ``__del__`` calls ``close()``, which reads attributes
    ``__init__`` never assigned. Left alone it is finalised at some arbitrary
    later collection — potentially inside a Qt paint or at interpreter
    shutdown, where this codebase has a history of native crashes. Collecting
    it here pins that to a known-safe point. The hook drops only the
    AttributeError that the upstream ``__del__`` raises; anything else is
    passed on. Inspecting corrupt files is this module's normal workload, so
    this path is common rather than exceptional.
    """
    previous = sys.unraisablehook

    def _ignore_broken_del(unraisable) -> None:
        if isinstance(unraisable.exc_value, AttributeError):
            return
        previous(unraisable)

    sys.unraisablehook = _ignore_broken_del
    try:
        gc.collect()
    finally:
        sys.unraisablehook = previous


_MDF4_ID = struct.Struct("<8s8s8s4sH30s2H")
_MDF4_BLOCK_HEADER = struct.Struct("<4s4sQQ")
_MDF4_HEADER_ADDRESS = 64
_MAX_BLOCK_LINKS = 32
_MAX_RAW_DATA_GROUPS = 64
_MAX_RAW_CHANNEL_GROUPS = 512
_MAX_CAN_GROUPS = 16
_MAX_CHANNEL_LINES = 40
_PROBE_RECORDS = 3

_UNFINALIZED_FLAGS = {
    0x01: "update-CG-counter",
    0x02: "update-SR-counter",
    0x04: "update-last-DT-length",
    0x08: "update-last-RD-length",
    0x10: "update-last-DL",
    0x20: "update-VLSD-bytes",
    0x40: "update-VLSD-offsets",
}


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not installed"
    except Exception as exc:
        return f"unavailable ({type(exc).__name__})"


def _clean_bytes(value: bytes) -> str:
    return value.rstrip(b"\0 ").decode("ascii", errors="replace") or "(empty)"


def _address(value: object) -> str:
    try:
        return f"0x{int(value):X}"
    except (TypeError, ValueError):
        return "n/a"


def _value(value: object, limit: int = 120) -> str:
    try:
        if hasattr(value, "item"):
            value = value.item()
    except Exception:
        pass
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _redact(text: object, paths: Iterable[str | Path]) -> str:
    result = str(text)
    for raw_path in paths:
        if not str(raw_path):
            continue
        path = Path(raw_path)
        if not path.name:
            continue
        candidates = {
            str(path),
            str(path.resolve()) if path.exists() else str(path),
            str(path).replace("\\", "/"),
        }
        for candidate in sorted(candidates, key=len, reverse=True):
            if candidate:
                result = result.replace(candidate, path.name)
    common_roots = (
        (str(Path.cwd()), "%APP%"),
        (str(Path.home()), "%USERPROFILE%"),
        (str(Path(sys.executable).resolve().parent), "%PYTHON%"),
    )
    for root, replacement in sorted(common_roots, key=lambda item: len(item[0]), reverse=True):
        result = result.replace(root, replacement)
        result = result.replace(root.replace("\\", "/"), replacement)
    return result


def _source_path(value: object) -> str:
    """Display logical source paths while hiding host filesystem directories."""
    text = str(value or "")
    if not text:
        return "''"
    if ":\\" in text or text.startswith("\\\\"):
        return repr(Path(text).name)
    return _value(text)


def _exception_text(exc: BaseException, paths: Iterable[str | Path]) -> str:
    chain: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(
            f"{type(current).__module__}.{type(current).__name__}: "
            f"{_redact(current, paths)}"
        )
        current = current.__cause__ or current.__context__
    return " <- ".join(chain)


def _edge_crc32(path: Path) -> tuple[str, str]:
    chunk = 64 * 1024
    with path.open("rb") as stream:
        head = stream.read(chunk)
        stream.seek(max(0, path.stat().st_size - chunk))
        tail = stream.read(chunk)
    return f"{zlib.crc32(head):08X}", f"{zlib.crc32(tail):08X}"


def _sample_positions(cycles: int) -> list[int]:
    if cycles <= 0:
        return []
    return sorted({0, cycles // 2, cycles - 1})


def _block_header(path: Path, address: int) -> dict[str, object]:
    size = path.stat().st_size
    result: dict[str, object] = {
        "address": address,
        "in_file": 0 <= address <= max(0, size - _MDF4_BLOCK_HEADER.size),
    }
    if not result["in_file"]:
        result["error"] = f"header outside file (size=0x{size:X})"
        return result

    with path.open("rb") as stream:
        stream.seek(address)
        raw = stream.read(_MDF4_BLOCK_HEADER.size)
        if len(raw) != _MDF4_BLOCK_HEADER.size:
            result["error"] = f"short header ({len(raw)} bytes)"
            return result
        block_id, _reserved, block_len, links_nr = _MDF4_BLOCK_HEADER.unpack(raw)
        result.update(
            block_id=_clean_bytes(block_id),
            block_len=int(block_len),
            links_nr=int(links_nr),
            aligned=(address % 8 == 0),
            complete=(
                block_len >= _MDF4_BLOCK_HEADER.size
                and address + block_len <= size
            ),
        )

        if links_nr > _MAX_BLOCK_LINKS:
            result["links_error"] = (
                f"declares {links_nr} links; capped at {_MAX_BLOCK_LINKS}"
            )
            links_to_read = _MAX_BLOCK_LINKS
        else:
            links_to_read = int(links_nr)
        links_raw = stream.read(links_to_read * 8)
        if len(links_raw) == links_to_read * 8:
            result["links"] = list(
                struct.unpack(f"<{links_to_read}Q", links_raw)
            ) if links_to_read else []
        else:
            result["links_error"] = f"short link table ({len(links_raw)} bytes)"
            result["links"] = []
    return result


def _block_line(header: Mapping[str, object]) -> str:
    if "error" in header:
        return (
            f"{_address(header.get('address'))} "
            f"FAIL {header['error']}"
        )
    state = "PASS" if header.get("complete") else "FAIL"
    link_text = ",".join(
        _address(item) for item in header.get("links", []) if int(item)
    ) or "none"
    extra = f" | {header['links_error']}" if header.get("links_error") else ""
    return (
        f"{_address(header.get('address'))} {state} "
        f"id={header.get('block_id')} len={header.get('block_len')} "
        f"links={header.get('links_nr')} [{link_text}] "
        f"aligned={'YES' if header.get('aligned') else 'NO'}{extra}"
    )


def _raw_block_problem(
    header: Mapping[str, object],
    expected_id: str,
    minimum_links: int,
) -> str | None:
    """Return the first structural problem in a raw MDF4 block header."""
    if "error" in header:
        return str(header["error"])
    if not header.get("aligned"):
        return "target address is not 8-byte aligned"
    if header.get("block_id") != expected_id:
        return (
            f"expected {expected_id}, found "
            f"{header.get('block_id') or '(empty block id)'}"
        )
    if not header.get("complete"):
        return (
            f"block extends outside file "
            f"(declared length={header.get('block_len')})"
        )

    links_nr = int(header.get("links_nr", 0) or 0)
    if links_nr < minimum_links:
        return (
            f"{expected_id} declares {links_nr} links; "
            f"at least {minimum_links} required"
        )
    if links_nr > _MAX_BLOCK_LINKS:
        return str(
            header.get("links_error")
            or f"declares too many links ({links_nr})"
        )

    block_len = int(header.get("block_len", 0) or 0)
    required_len = _MDF4_BLOCK_HEADER.size + links_nr * 8
    if block_len < required_len:
        return (
            f"declared length {block_len} is shorter than its "
            f"{links_nr}-entry link table ({required_len} bytes required)"
        )
    if len(header.get("links", [])) != links_nr:
        return str(header.get("links_error") or "incomplete link table")
    return None


def _raw_pointer_line(
    source_id: str,
    source_address: int,
    link_index: int,
    link_name: str,
    target_address: int,
) -> str:
    return (
        f"{source_id}.{link_name}: source={_address(source_address)} "
        f"link[{link_index}] target={_address(target_address)}"
    )


def _inspect_mdf4_hd_dg_cg(
    path: Path,
) -> tuple[list[str], str, str]:
    """Inspect the raw MDF4 HD -> DG -> CG links without using a parser.

    Only the common block headers and the standard link positions needed for
    this chain are read. Every read is file-bounded, every linked target is
    type-checked, and cycles plus excessive chains are capped.
    """
    file_size = path.stat().st_size
    lines = [
        "RAW MDF4 ##HD -> ##DG -> ##CG LINK VALIDATION",
        (
            f"  File bounds: 0x0.."
            f"{max(0, file_size - 1):X} ({file_size:,} bytes)"
        ),
        "  Link layout: ##HD[0]=first ##DG; "
        "##DG[0]=next ##DG, ##DG[1]=first ##CG; "
        "##CG[0]=next ##CG",
    ]
    failures: list[str] = []
    warnings: list[str] = []
    data_group_count = 0
    channel_group_count = 0

    hd = _block_header(path, _MDF4_HEADER_ADDRESS)
    hd_problem = _raw_block_problem(hd, "##HD", 1)
    lines.append(f"  ##HD {_block_line(hd)}")
    if hd_problem:
        detail = (
            f"fixed ##HD address {_address(_MDF4_HEADER_ADDRESS)}: "
            f"{hd_problem}"
        )
        lines.append(f"  EXACT STRUCTURAL FAILURE: {detail}")
        return lines, "FAIL", detail

    first_dg = int(hd["links"][0])
    lines.append(
        "  "
        + _raw_pointer_line(
            "##HD", _MDF4_HEADER_ADDRESS, 0, "first_dg", first_dg
        )
        + (" (END - no data groups)" if not first_dg else "")
    )

    dg_address = first_dg
    dg_source_id = "##HD"
    dg_source_address = _MDF4_HEADER_ADDRESS
    dg_source_link = 0
    dg_source_name = "first_dg"
    seen_dg: set[int] = set()
    seen_cg_global: set[int] = set()

    while dg_address:
        pointer = _raw_pointer_line(
            dg_source_id,
            dg_source_address,
            dg_source_link,
            dg_source_name,
            dg_address,
        )
        if dg_address in seen_dg:
            detail = f"{pointer}: cycle to an already visited ##DG"
            lines.append(f"  EXACT CORRUPT POINTER: {detail}")
            failures.append(detail)
            break
        if data_group_count >= _MAX_RAW_DATA_GROUPS:
            detail = (
                f"stopped after {_MAX_RAW_DATA_GROUPS} data groups at "
                f"{pointer}; safety cap reached"
            )
            lines.append(f"  WARN {detail}")
            warnings.append(detail)
            break

        seen_dg.add(dg_address)
        dg = _block_header(path, dg_address)
        dg_problem = _raw_block_problem(dg, "##DG", 2)
        lines.append(
            f"  DG[{data_group_count}] target from {pointer}\n"
            f"    {_block_line(dg)}"
        )
        if dg_problem:
            detail = f"{pointer}: {dg_problem}"
            lines.append(f"    EXACT CORRUPT POINTER: {detail}")
            failures.append(detail)
            break

        data_group_count += 1
        dg_links = [int(item) for item in dg["links"]]
        next_dg = dg_links[0]
        first_cg = dg_links[1]
        lines.append(
            "    "
            + _raw_pointer_line("##DG", dg_address, 1, "first_cg", first_cg)
            + (" (END - no channel groups)" if not first_cg else "")
        )

        cg_address = first_cg
        cg_source_address = dg_address
        cg_source_id = "##DG"
        cg_source_link = 1
        cg_source_name = "first_cg"
        seen_cg_in_dg: set[int] = set()
        while cg_address:
            pointer = _raw_pointer_line(
                cg_source_id,
                cg_source_address,
                cg_source_link,
                cg_source_name,
                cg_address,
            )
            if cg_address in seen_cg_in_dg:
                detail = f"{pointer}: cycle to an already visited ##CG"
                lines.append(f"    EXACT CORRUPT POINTER: {detail}")
                failures.append(detail)
                break
            if cg_address in seen_cg_global:
                detail = f"{pointer}: ##CG is referenced by multiple data groups"
                lines.append(f"    EXACT CORRUPT POINTER: {detail}")
                failures.append(detail)
                break
            if channel_group_count >= _MAX_RAW_CHANNEL_GROUPS:
                detail = (
                    f"stopped after {_MAX_RAW_CHANNEL_GROUPS} channel groups "
                    f"at {pointer}; safety cap reached"
                )
                lines.append(f"    WARN {detail}")
                warnings.append(detail)
                break

            seen_cg_in_dg.add(cg_address)
            seen_cg_global.add(cg_address)
            cg = _block_header(path, cg_address)
            cg_problem = _raw_block_problem(cg, "##CG", 1)
            lines.append(
                f"    CG[{channel_group_count}] target from {pointer}\n"
                f"      {_block_line(cg)}"
            )
            if cg_problem:
                detail = f"{pointer}: {cg_problem}"
                lines.append(f"      EXACT CORRUPT POINTER: {detail}")
                failures.append(detail)
                break

            channel_group_count += 1
            next_cg = int(cg["links"][0])
            lines.append(
                "      "
                + _raw_pointer_line(
                    "##CG", cg_address, 0, "next_cg", next_cg
                )
                + (" (END)" if not next_cg else "")
            )
            cg_source_id = "##CG"
            cg_source_address = cg_address
            cg_source_link = 0
            cg_source_name = "next_cg"
            cg_address = next_cg

        lines.append(
            "    "
            + _raw_pointer_line("##DG", dg_address, 0, "next_dg", next_dg)
            + (" (END)" if not next_dg else "")
        )
        dg_source_id = "##DG"
        dg_source_address = dg_address
        dg_source_link = 0
        dg_source_name = "next_dg"
        dg_address = next_dg

    lines.append(
        f"  Raw chain totals: data_groups={data_group_count}, "
        f"channel_groups={channel_group_count}"
    )
    if failures:
        return lines, "FAIL", failures[0]
    if warnings:
        return lines, "WARN", warnings[0]
    return (
        lines,
        "PASS",
        f"data_groups={data_group_count}, channel_groups={channel_group_count}",
    )


def _walk_signal_blocks(path: Path, start_address: int) -> list[dict[str, object]]:
    """Follow the bounded generic link graph rooted at a signal-data block."""
    pending = [int(start_address)] if start_address else []
    visited: set[int] = set()
    headers: list[dict[str, object]] = []
    while pending and len(headers) < _MAX_BLOCK_LINKS:
        address = pending.pop(0)
        if not address or address in visited:
            continue
        visited.add(address)
        header = _block_header(path, address)
        headers.append(header)
        block_id = str(header.get("block_id", ""))
        # Signal data can be direct (SD/DZ) or routed through HL/DL.  Only
        # traverse recognised data-chain blocks; arbitrary MDF links may point
        # into unrelated metadata and would make the report noisy.
        if block_id in {"##HL", "##DL"}:
            for link in header.get("links", []):
                link_value = int(link)
                if link_value and link_value not in visited:
                    pending.append(link_value)
    return headers


def _format_identification(path: Path) -> tuple[list[str], int | None]:
    lines = ["MDF IDENTIFICATION"]
    with path.open("rb") as stream:
        raw = stream.read(_MDF4_ID.size)
    if len(raw) != _MDF4_ID.size:
        return lines + [f"  FAIL short identification block: {len(raw)} bytes"], None

    (
        file_id,
        version_text,
        program_id,
        _reserved0,
        mdf_version,
        _reserved1,
        standard_flags,
        custom_flags,
    ) = _MDF4_ID.unpack(raw)
    version_number = int(mdf_version)
    flag_names = [
        name for bit, name in _UNFINALIZED_FLAGS.items()
        if int(standard_flags) & bit
    ]
    lines.extend(
        [
            f"  File ID: {_clean_bytes(file_id)}",
            f"  Version: {_clean_bytes(version_text)} ({version_number})",
            f"  Creator: {_clean_bytes(program_id)}",
            (
                f"  Finalized: {'YES' if not standard_flags and not custom_flags else 'NO'}"
                f" | standard=0x{int(standard_flags):04X}"
                f" custom=0x{int(custom_flags):04X}"
            ),
            f"  Pending finalization: {', '.join(flag_names) if flag_names else 'none'}",
        ]
    )
    return lines, version_number


class _Inspection:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[str] = []
        self.probes: list[tuple[str, str, str]] = []
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.fixed_fields_readable = False
        self.vlsd_seen = False
        self.vlsd_offsets_readable = False
        self.signal_blocks_resolved = False
        self.payload_readable = False
        self.can_groups = 0

    def probe(self, name: str, status: str, detail: str = "") -> None:
        self.probes.append((name, status, detail))
        if status == "FAIL":
            self.failures.append(f"{name}: {detail}")
        elif status == "WARN":
            self.warnings.append(f"{name}: {detail}")

    def _probe_scalar(
        self,
        source,
        group_index: int,
        channel_index: int,
        channel_name: str,
        cycles: int,
    ) -> None:
        samples: list[str] = []
        dtype = "?"
        shape = "?"
        try:
            for record_offset in _sample_positions(cycles):
                signal = source.get(
                    group=group_index,
                    index=channel_index,
                    raw=True,
                    record_offset=record_offset,
                    record_count=1,
                )
                arr = signal.samples
                dtype = str(getattr(arr, "dtype", "?"))
                shape = str(getattr(arr, "shape", "?"))
                samples.append(
                    f"{record_offset}:{_value(arr[0]) if len(arr) else '(empty)'}"
                )
            self.fixed_fields_readable = True
            self.probe(f"G{group_index} {channel_name}", "PASS", "bounded read")
            self.lines.append(
                f"    READ PASS dtype={dtype} shape={shape} "
                f"samples=[{', '.join(samples)}]"
            )
        except Exception as exc:
            detail = _exception_text(exc, [self.path])
            self.probe(f"G{group_index} {channel_name}", "FAIL", detail)
            self.lines.append(f"    READ FAIL {detail}")

    def _probe_vlsd_offsets(
        self,
        source,
        group,
        group_index: int,
        channel_index: int,
        cycles: int,
    ) -> None:
        backend = getattr(source, "_mdf", source)
        method = getattr(backend, "_get_scalar", None)
        if method is None:
            detail = "installed asammdf does not expose bounded VLSD offset access"
            self.probe(f"G{group_index} VLSD offsets", "WARN", detail)
            self.lines.append(f"    OFFSET WARN {detail}")
            return

        offsets: list[str] = []
        numeric_offsets: list[int] = []
        try:
            channel = group.channels[channel_index]
            dependencies = group.channel_dependencies[channel_index]
            for record_offset in _sample_positions(cycles):
                vals, _timestamps, _invalid, _encoding = method(
                    channel=channel,
                    group=group,
                    group_index=group_index,
                    channel_index=channel_index,
                    dependency_list=dependencies,
                    raster=None,
                    data=None,
                    ignore_invalidation_bits=True,
                    record_offset=record_offset,
                    record_count=1,
                    master_is_required=False,
                    skip_vlsd=True,
                )
                if len(vals):
                    item = int(vals[0])
                    numeric_offsets.append(item)
                    offsets.append(f"{record_offset}:0x{item:X}")
                else:
                    offsets.append(f"{record_offset}:(empty)")
            self.vlsd_offsets_readable = True
            sampled_order = all(
                right >= left
                for left, right in zip(numeric_offsets, numeric_offsets[1:])
            )
            status = "PASS" if sampled_order else "WARN"
            detail = (
                f"sampled offsets [{', '.join(offsets)}], "
                f"sampled monotonic={'YES' if sampled_order else 'NO'}"
            )
            self.probe(f"G{group_index} VLSD offsets", status, detail)
            self.lines.append(f"    OFFSET {status} {detail}")
        except Exception as exc:
            detail = _exception_text(exc, [self.path])
            self.probe(f"G{group_index} VLSD offsets", "FAIL", detail)
            self.lines.append(f"    OFFSET FAIL {detail}")

    def _probe_signal_data_blocks(
        self,
        group,
        group_index: int,
        channel_index: int,
        channel,
    ) -> None:
        address = int(getattr(channel, "data_block_addr", 0) or 0)
        signal_data = None
        try:
            signal_data = group.signal_data[channel_index]
        except Exception:
            pass
        self.lines.append(
            f"    SIGNAL DATA reference={_address(address)} "
            f"semantic_entry={'present' if signal_data else 'missing'}"
        )

        headers = _walk_signal_blocks(self.path, address)
        if not headers:
            self.probe(
                f"G{group_index} signal-data link",
                "FAIL",
                f"no usable block at {_address(address)}",
            )
            self.lines.append("    BLOCK CHAIN FAIL no signal-data block resolved")
        else:
            for header in headers:
                self.lines.append(f"      {_block_line(header)}")
            bad = [
                header for header in headers
                if "error" in header or not header.get("complete")
            ]
            root_type = str(headers[0].get("block_id", ""))
            recognised = root_type in {"##SD", "##DZ", "##DL", "##HL"}
            if bad or not recognised:
                detail = (
                    _block_line(bad[0]) if bad
                    else f"unexpected root block type {root_type}"
                )
                self.probe(f"G{group_index} signal-data link", "FAIL", detail)
            else:
                self.signal_blocks_resolved = True
                self.probe(
                    f"G{group_index} signal-data link",
                    "PASS",
                    f"root={root_type}, blocks={len(headers)}",
                )

        try:
            infos = list(group.get_signal_data_blocks(channel_index))
            if not infos:
                self.lines.append("    ASAMMDF BLOCK MAP: empty")
                self.probe(
                    f"G{group_index} asammdf signal-data map",
                    "FAIL",
                    "channel has VLSD offsets but asammdf resolved no payload blocks",
                )
            else:
                self.lines.append(
                    f"    ASAMMDF BLOCK MAP: {len(infos)} payload block(s)"
                )
                for info in infos[:_MAX_BLOCK_LINKS]:
                    self.lines.append(
                        "      "
                        f"address={_address(getattr(info, 'address', 0))} "
                        f"original={getattr(info, 'original_size', '?')} "
                        f"compressed={getattr(info, 'compressed_size', '?')} "
                        f"type={getattr(info, 'block_type', '?')} "
                        f"param={getattr(info, 'param', '?')} "
                        f"location={getattr(info, 'location', '?')}"
                    )
                self.probe(
                    f"G{group_index} asammdf signal-data map",
                    "PASS",
                    f"{len(infos)} block(s)",
                )
        except Exception as exc:
            detail = _exception_text(exc, [self.path])
            self.lines.append(f"    ASAMMDF BLOCK MAP FAIL {detail}")
            self.probe(
                f"G{group_index} asammdf signal-data map", "FAIL", detail
            )

    def _probe_payload(
        self,
        source,
        group_index: int,
        channel_index: int,
        cycles: int,
    ) -> None:
        observations: list[str] = []
        try:
            for record_offset in _sample_positions(cycles):
                signal = source.get(
                    group=group_index,
                    index=channel_index,
                    raw=True,
                    record_offset=record_offset,
                    record_count=1,
                )
                samples = signal.samples
                if len(samples):
                    row = bytes(samples[0])
                    observations.append(
                        f"{record_offset}:len={len(row)},crc32={zlib.crc32(row):08X}"
                    )
                else:
                    observations.append(f"{record_offset}:(empty)")
            self.payload_readable = True
            detail = f"bounded payload samples [{', '.join(observations)}]"
            self.probe(f"G{group_index} DataBytes payload", "PASS", detail)
            self.lines.append(f"    PAYLOAD PASS {detail}")
        except Exception as exc:
            detail = _exception_text(exc, [self.path])
            self.probe(f"G{group_index} DataBytes payload", "FAIL", detail)
            self.lines.append(f"    PAYLOAD FAIL {detail}")

    def inspect_can_group(self, source, group_index: int, group) -> None:
        self.can_groups += 1
        cg = group.channel_group
        dg = getattr(group, "data_group", None)
        acq = getattr(cg, "acq_source", None)
        cycles = int(getattr(cg, "cycles_nr", 0) or 0)
        dg_data_address = int(getattr(dg, "data_block_addr", 0) or 0)
        self.lines.extend(
            [
                "",
                f"CAN GROUP {group_index}",
                (
                    f"  CG address={_address(getattr(cg, 'address', 0))} "
                    f"flags=0x{int(getattr(cg, 'flags', 0) or 0):X} "
                    f"cycles={cycles:,} "
                    f"record_bytes={getattr(cg, 'samples_byte_nr', '?')} "
                    f"invalidation_bytes={getattr(cg, 'invalidation_bytes_nr', '?')}"
                ),
                (
                    f"  DG address={_address(getattr(dg, 'address', 0))} "
                    f"data={_address(getattr(dg, 'data_block_addr', 0))} "
                    f"record_id_len={getattr(dg, 'record_id_len', '?')}"
                ),
                (
                    f"  SOURCE name={_value(getattr(acq, 'name', ''))} "
                    f"path={_source_path(getattr(acq, 'path', ''))} "
                    f"source_type={getattr(acq, 'source_type', '?')} "
                    f"bus_type={getattr(acq, 'bus_type', '?')}"
                ),
            ]
        )

        if dg_data_address:
            data_headers = _walk_signal_blocks(self.path, dg_data_address)
            self.lines.append("  DATA-GROUP BLOCK CHAIN")
            for header in data_headers:
                self.lines.append(f"    {_block_line(header)}")
            bad = [
                header for header in data_headers
                if "error" in header or not header.get("complete")
            ]
            root_type = (
                str(data_headers[0].get("block_id", ""))
                if data_headers else ""
            )
            if (
                not data_headers
                or bad
                or root_type not in {"##DT", "##DZ", "##DL", "##HL", "##RD"}
            ):
                detail = (
                    _block_line(bad[0]) if bad
                    else f"unexpected/missing root block {root_type or '(none)'}"
                )
                self.probe(f"G{group_index} data-group block chain", "FAIL", detail)
            else:
                self.probe(
                    f"G{group_index} data-group block chain",
                    "PASS",
                    f"root={root_type}, blocks={len(data_headers)}",
                )
        elif cycles:
            self.probe(
                f"G{group_index} data-group block chain",
                "FAIL",
                "non-empty channel group has a null data-block address",
            )

        try:
            group.load_all_data_blocks()
            data_infos = list(getattr(group, "data_blocks", []) or [])
            expected = cycles * (
                int(getattr(cg, "samples_byte_nr", 0) or 0)
                + int(getattr(cg, "invalidation_bytes_nr", 0) or 0)
            )
            logical = sum(
                int(getattr(info, "original_size", 0) or 0)
                for info in data_infos
            )
            self.lines.append(
                f"  DATA-GROUP MAP blocks={len(data_infos)} "
                f"logical_bytes={logical:,} expected_record_bytes={expected:,}"
            )
            for info in data_infos[:_MAX_BLOCK_LINKS]:
                self.lines.append(
                    "    "
                    f"address={_address(getattr(info, 'address', 0))} "
                    f"original={getattr(info, 'original_size', '?')} "
                    f"compressed={getattr(info, 'compressed_size', '?')} "
                    f"type={getattr(info, 'block_type', '?')} "
                    f"param={getattr(info, 'param', '?')} "
                    f"location={getattr(info, 'location', '?')}"
                )
            if cycles and logical < expected:
                self.probe(
                    f"G{group_index} record-byte coverage",
                    "WARN",
                    f"logical={logical:,}, expected-at-least={expected:,}",
                )
            elif cycles:
                self.probe(
                    f"G{group_index} record-byte coverage",
                    "PASS",
                    f"logical={logical:,}, expected={expected:,}",
                )
        except Exception as exc:
            detail = _exception_text(exc, [self.path])
            self.lines.append(f"  DATA-GROUP MAP FAIL {detail}")
            self.probe(f"G{group_index} data-group map", "FAIL", detail)

        backend = getattr(source, "_mdf", source)
        master_index = getattr(backend, "masters_db", {}).get(group_index)
        if master_index is not None and 0 <= int(master_index) < len(group.channels):
            master = group.channels[int(master_index)]
            self.lines.append(
                "  MASTER "
                f"[{int(master_index)}] {getattr(master, 'name', '?')} "
                f"channel_type={getattr(master, 'channel_type', '?')} "
                f"sync_type={getattr(master, 'sync_type', '?')} "
                f"data_type={getattr(master, 'data_type', '?')} "
                f"dtype={getattr(master, 'dtype_fmt', '?')} "
                f"unit={_value(getattr(master, 'unit', ''))} "
                f"byte.bit={getattr(master, 'byte_offset', '?')}."
                f"{getattr(master, 'bit_offset', '?')} "
                f"bits={getattr(master, 'bit_count', '?')}"
            )
            if cycles:
                self._probe_scalar(
                    source,
                    group_index,
                    int(master_index),
                    f"master {getattr(master, 'name', '?')}",
                    cycles,
                )

        interesting: list[tuple[int, object]] = []
        for channel_index, channel in enumerate(group.channels):
            name = str(getattr(channel, "name", "") or "")
            if (
                name == "CAN_DataFrame"
                or name.startswith("CAN_DataFrame.")
                or name.startswith("CAN_ErrorFrame")
                or name.startswith("CAN_RemoteFrame")
            ):
                interesting.append((channel_index, channel))

        self.lines.append(
            f"  CHANNELS total={len(group.channels)} relevant={len(interesting)}"
        )
        if not interesting:
            self.probe(
                f"G{group_index} CAN structure",
                "FAIL",
                "bus-event group has no recognised CAN frame channels",
            )
            return

        for channel_index, channel in interesting[:_MAX_CHANNEL_LINES]:
            name = str(getattr(channel, "name", "") or "")
            dependencies = group.channel_dependencies[channel_index]
            channel_source = getattr(channel, "source", None)
            self.lines.append(
                "  "
                f"[{channel_index}] {name} | "
                f"channel_type={getattr(channel, 'channel_type', '?')} "
                f"data_type={getattr(channel, 'data_type', '?')} "
                f"dtype={getattr(channel, 'dtype_fmt', '?')} "
                f"byte.bit={getattr(channel, 'byte_offset', '?')}."
                f"{getattr(channel, 'bit_offset', '?')} "
                f"bits={getattr(channel, 'bit_count', '?')} "
                f"flags=0x{int(getattr(channel, 'flags', 0) or 0):X} "
                f"invalid_bit={getattr(channel, 'pos_invalidation_bit', '?')} "
                f"data={_address(getattr(channel, 'data_block_addr', 0))} "
                f"deps={len(dependencies or [])}"
            )
            self.lines.append(
                "    META "
                f"cn={_address(getattr(channel, 'address', 0))} "
                f"block_len={getattr(channel, 'block_len', '?')} "
                f"links={getattr(channel, 'links_nr', '?')} "
                f"component={_address(getattr(channel, 'component_addr', 0))} "
                f"conversion={_address(getattr(channel, 'conversion_addr', 0))} "
                f"source={_address(getattr(channel, 'source_addr', 0))} "
                f"sync={getattr(channel, 'sync_type', '?')} "
                f"unit={_value(getattr(channel, 'unit', ''))} "
                f"limits={_value((getattr(channel, 'lower_limit', None), getattr(channel, 'upper_limit', None)))} "
                f"source_name={_value(getattr(channel_source, 'name', ''))} "
                f"source_path={_source_path(getattr(channel_source, 'path', ''))}"
            )

            if name.endswith(".DataBytes") or name == "CAN_DataFrame.DataBytes":
                channel_type = int(
                    getattr(channel, "channel_type", -1) or 0
                )
                if channel_type == 1 and cycles:
                    self.vlsd_seen = True
                    self.lines.append("    STORAGE variable-length (VLSD)")
                    self._probe_vlsd_offsets(
                        source, group, group_index, channel_index, cycles
                    )
                    self._probe_signal_data_blocks(
                        group, group_index, channel_index, channel
                    )
                else:
                    self.lines.append(
                        "    STORAGE "
                        + (
                            "variable-length (VLSD), empty group"
                            if channel_type == 1
                            else "fixed-width in the channel-group record"
                        )
                    )
                if cycles:
                    self._probe_payload(
                        source, group_index, channel_index, cycles
                    )
            elif cycles and name != "CAN_DataFrame" and not dependencies:
                self._probe_scalar(
                    source, group_index, channel_index, name, cycles
                )

        if len(interesting) > _MAX_CHANNEL_LINES:
            self.lines.append(
                f"  ... {len(interesting) - _MAX_CHANNEL_LINES} relevant channels omitted"
            )

        parent = next(
            (
                channel_index
                for channel_index, channel in interesting
                if getattr(channel, "name", "") == "CAN_DataFrame"
            ),
            None,
        )
        if parent is not None:
            try:
                for record_offset in _sample_positions(cycles):
                    source.get(
                        group=group_index,
                        index=parent,
                        raw=True,
                        record_offset=record_offset,
                        record_count=1,
                    )
                self.probe(
                    f"G{group_index} compound CAN_DataFrame",
                    "PASS",
                    "first/middle/last records",
                )
                self.lines.append("  COMPOUND READ PASS first/middle/last records")
            except Exception as exc:
                detail = _exception_text(exc, [self.path])
                self.probe(
                    f"G{group_index} compound CAN_DataFrame", "FAIL", detail
                )
                self.lines.append(f"  COMPOUND READ FAIL {detail}")

    def result_text(self, body: list[str]) -> str:
        status = "FAIL" if self.failures else "WARN" if self.warnings else "PASS"
        failure_text = self.failures[0] if self.failures else (
            self.warnings[0] if self.warnings else "No structural problem found"
        )
        if any("VLSD" in item or "DataBytes" in item or "signal-data" in item
               for item in self.failures):
            classification = "MDF-VLSD-PAYLOAD"
            layer = "MDF variable-length CAN payload storage"
            dbc_involved = "NO - payload failed before DBC decoding"
        elif any("Raw MDF4 HD-DG-CG links" in item for item in self.failures):
            classification = "MDF-RAW-BLOCK-LINK"
            layer = "MDF4 HD/DG/CG structural links"
            dbc_involved = "NO - file structure failed before DBC decoding"
        elif self.failures:
            classification = "MDF-STRUCTURE-OR-READER"
            layer = "MDF/CAN measurement loading"
            dbc_involved = "UNKNOWN"
        else:
            classification = "NO-STRUCTURAL-FAILURE"
            layer = "No failing measurement layer found"
            dbc_involved = "NOT YET ASSESSED"

        summary = [
            "CANScope LOAD DEBUG",
            "=" * 100,
            f"STATUS: {status}",
            f"CLASSIFICATION: {classification}",
            f"FAILURE LAYER: {layer}",
            f"DBC INVOLVED: {dbc_involved}",
            f"FIRST EVIDENCE: {failure_text}",
            (
                "RECOVERY SIGNALS: "
                f"fixed_fields={'YES' if self.fixed_fields_readable else 'NO'}, "
                f"vlsd_offsets={('YES' if self.vlsd_offsets_readable else 'NO') if self.vlsd_seen else 'N/A'}, "
                f"block_chain={('YES' if self.signal_blocks_resolved else 'NO') if self.vlsd_seen else 'N/A'}, "
                f"payload={'YES' if self.payload_readable else 'NO'}"
            ),
        ]
        evidence = self.failures or self.warnings
        if evidence:
            summary.append("ANOMALY EVIDENCE (complete details continue below)")
            summary.extend(
                f"  {index}. {item[:260]}"
                for index, item in enumerate(evidence[:8], 1)
            )
            if len(evidence) > 8:
                summary.append(
                    f"  ... {len(evidence) - 8} additional anomaly entries"
                )
        summary.append("")
        matrix = ["", "PROBE MATRIX"]
        matrix.extend(
            f"  {status_:4} {name}"
            + (f" | {detail}" if detail else "")
            for name, status_, detail in self.probes
        )
        return "\n".join(summary + body + matrix)


def inspect_measurement(
    measurement_path: str | Path,
    *,
    app_version: str = "",
) -> str:
    """Return a deep, bounded plain-text inspection of a measurement file."""
    path = Path(measurement_path)
    inspection = _Inspection(path)
    body: list[str] = [
        "ENVIRONMENT",
        f"  CANScope: {app_version or '(unknown)'}",
        (
            f"  OS: {platform.system()} {platform.release()} "
            f"{platform.machine()} | Python: {platform.python_version()}"
        ),
        (
            f"  asammdf: {_package_version('asammdf')} | "
            f"python-can: {_package_version('python-can')} | "
            f"cantools: {_package_version('cantools')} | "
            f"numpy: {_package_version('numpy')}"
        ),
        "",
        "MEASUREMENT",
        f"  File: {path.name}",
    ]
    if not path.exists():
        inspection.probe("Measurement file", "FAIL", "file not found")
        body.append("  FAIL file not found")
        return inspection.result_text(body)

    stat = path.stat()
    try:
        head_crc, tail_crc = _edge_crc32(path)
    except Exception as exc:
        head_crc = tail_crc = f"unavailable ({type(exc).__name__})"
    body.extend(
        [
            f"  Size: {stat.st_size:,} bytes (0x{stat.st_size:X})",
            f"  Modified: {datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')}",
            f"  Edge CRC32: head={head_crc} tail={tail_crc}",
        ]
    )
    inspection.probe("Measurement file", "PASS", f"{stat.st_size:,} bytes")

    suffix = path.suffix.lower()
    if suffix not in {".mf4", ".mdf"}:
        body.extend(
            [
                "",
                "FORMAT",
                f"  Deep binary inspection currently targets MDF/MF4; selected {suffix or '(none)'}.",
                "  The normal Load + Decode runtime will still be captured below.",
            ]
        )
        inspection.probe("Deep MDF probe", "WARN", f"not applicable to {suffix}")
        return inspection.result_text(body)

    try:
        id_lines, mdf_version = _format_identification(path)
        body.extend([""] + id_lines)
        inspection.probe("Direct MDF identification", "PASS", id_lines[2].strip())
        if mdf_version is not None and mdf_version < 400:
            body.append("  WARN MDF3 detected; MDF4 block-link probe is not applicable.")
            inspection.probe(
                "Direct MDF4 block links", "WARN", f"MDF version {mdf_version}"
            )
        elif mdf_version is not None:
            raw_lines, raw_status, raw_detail = _inspect_mdf4_hd_dg_cg(path)
            body.extend([""] + raw_lines)
            inspection.probe(
                "Raw MDF4 HD-DG-CG links",
                raw_status,
                raw_detail,
            )
    except Exception as exc:
        detail = _exception_text(exc, [path])
        body.extend(["", "MDF IDENTIFICATION", f"  FAIL {detail}"])
        inspection.probe("Direct MDF identification", "FAIL", detail)

    source = None
    try:
        import asammdf

        source = asammdf.MDF(str(path), use_display_names=False)
        backend = getattr(source, "_mdf", source)
        groups = list(source.groups)
        bus_map = getattr(backend, "bus_logging_map", {}) or {}
        can_map = bus_map.get("CAN", {}) if isinstance(bus_map, dict) else {}
        body.extend(
            [
                "",
                "ASAMMDF METADATA",
                f"  Open: PASS | version={getattr(source, 'version', '?')}",
                f"  Groups: {len(groups)} | CAN bus map entries: {len(can_map)}",
                f"  CAN bus map: {_value(can_map, 500)}",
                f"  Start time: {_value(getattr(getattr(source, 'header', None), 'start_time', '?'))}",
            ]
        )
        inspection.probe("asammdf metadata open", "PASS", f"{len(groups)} groups")

        can_group_rows = []
        for group_index, group in enumerate(groups):
            names = {
                str(getattr(channel, "name", "") or "")
                for channel in group.channels
            }
            cg = group.channel_group
            acq = getattr(cg, "acq_source", None)
            is_bus_event = bool(int(getattr(cg, "flags", 0) or 0) & 0x2)
            is_can = (
                any(
                    name == "CAN_DataFrame"
                    or name.startswith("CAN_DataFrame.")
                    or name.startswith("CAN_ErrorFrame")
                    or name.startswith("CAN_RemoteFrame")
                    for name in names
                )
                or (
                    is_bus_event
                    and int(getattr(acq, "bus_type", -1) or -1) == 2
                )
            )
            if is_can:
                can_group_rows.append((group_index, group))

        body.append(
            f"  CAN groups detected independently: {len(can_group_rows)}"
        )
        if not can_group_rows:
            inspection.probe(
                "CAN bus-event groups", "FAIL", "no CAN_DataFrame structure found"
            )
        else:
            inspection.probe(
                "CAN bus-event groups", "PASS", str(len(can_group_rows))
            )
            for group_index, group in can_group_rows[:_MAX_CAN_GROUPS]:
                inspection.inspect_can_group(
                    source, group_index, group
                )
            if len(can_group_rows) > _MAX_CAN_GROUPS:
                inspection.lines.append(
                    f"\nWARN {len(can_group_rows) - _MAX_CAN_GROUPS} CAN groups "
                    f"omitted after the {_MAX_CAN_GROUPS}-group safety cap."
                )
                inspection.probe(
                    "CAN group safety cap",
                    "WARN",
                    f"{len(can_group_rows)} groups; inspected {_MAX_CAN_GROUPS}",
                )
    except Exception as exc:
        detail = _exception_text(exc, [path])
        body.extend(["", "ASAMMDF METADATA", f"  Open/inspect: FAIL {detail}"])
        inspection.probe("asammdf metadata open", "FAIL", detail)
        body.extend(
            [
                "  Independent MDF identification above remains valid.",
                "  Runtime traceback:",
                *[
                    f"    {line}"
                    for line in _redact(
                        "".join(traceback.format_exception(exc)), [path]
                    ).splitlines()[-12:]
                ],
            ]
        )
    finally:
        if source is not None:
            try:
                source.close()
            except Exception:
                pass
        else:
            # The open failed. Only now, with the handler's traceback released,
            # is the half-built MDF4 it left behind actually unreachable.
            _collect_failed_mdf_open()

    body.extend(inspection.lines)

    # Exercise the exact python-can path independently after closing the first
    # MDF handle.  Only one message is requested.
    reader = None
    try:
        import can

        reader = can.MF4Reader(str(path))
        try:
            message = next(iter(reader), None)
            if message is None:
                inspection.probe(
                    "python-can MF4Reader", "WARN", "no CAN message returned"
                )
                body.append("\nPYTHON-CAN PROBE\n  WARN no CAN message returned")
            else:
                inspection.probe(
                    "python-can MF4Reader",
                    "PASS",
                    (
                        f"first ID=0x{int(message.arbitration_id):X} "
                        f"DLC={getattr(message, 'dlc', '?')}"
                    ),
                )
                body.append(
                    "\nPYTHON-CAN PROBE\n"
                    f"  PASS first ID=0x{int(message.arbitration_id):X} "
                    f"DLC={getattr(message, 'dlc', '?')} "
                    f"channel={getattr(message, 'channel', '?')}"
                )
        finally:
            reader.stop()
    except Exception as exc:
        detail = _exception_text(exc, [path])
        inspection.probe("python-can MF4Reader", "FAIL", detail)
        body.append(f"\nPYTHON-CAN PROBE\n  FAIL {detail}")
    finally:
        if reader is None:
            # MF4Reader builds an MDF internally, so a constructor that raises
            # strands one the same way a direct open does.
            _collect_failed_mdf_open()

    return inspection.result_text(body)


def _database_candidates(decoder, frame_id: int) -> list[object]:
    candidates: list[object] = []
    seen: set[int] = set()
    for lookup in (frame_id, frame_id & 0x1FFFFFFF, frame_id & 0x7FF):
        for message in decoder._messages_exact.get(lookup, []):
            marker = id(message)
            if marker not in seen:
                seen.add(marker)
                candidates.append(message)
    if frame_id > 0x7FF:
        pgn = decoder._extract_j1939_pgn(frame_id)
        if pgn is not None:
            for message in decoder._messages_pgn.get(pgn, []):
                marker = id(message)
                if marker not in seen:
                    seen.add(marker)
                    candidates.append(message)
    return candidates


def inspect_databases(
    channel_mappings: Mapping[int, str],
    observed_ids: Mapping[int, Iterable[int]] | None = None,
) -> str:
    """Return a redacted text inspection of configured DBC/ARXML databases."""
    lines = [
        "",
        "=" * 100,
        "DATABASE INSPECTION",
        f"Configured mappings: {len(channel_mappings)}",
    ]
    if not channel_mappings:
        return "\n".join(lines + ["STATUS: WARN - no database configured"])

    from core.dbc_decoder import DBCDecoder

    observed = {
        int(channel): {int(frame_id) for frame_id in frame_ids}
        for channel, frame_ids in (observed_ids or {}).items()
    }
    unique_paths = list(dict.fromkeys(str(path) for path in channel_mappings.values()))
    path_to_channels: dict[str, list[int]] = {path: [] for path in unique_paths}
    fallback_path = channel_mappings.get(0)
    for channel, path in channel_mappings.items():
        if int(channel) != 0:
            path_to_channels[str(path)].append(int(channel))
    if fallback_path:
        for channel in observed:
            if channel not in channel_mappings:
                path_to_channels[str(fallback_path)].append(channel)

    overall = "PASS"
    for raw_path in unique_paths:
        path = Path(raw_path)
        assigned = sorted(set(path_to_channels.get(raw_path, [])))
        if raw_path == fallback_path:
            assignment = "All Channels" + (
                f" (observed {','.join(map(str, assigned))})" if assigned else ""
            )
        else:
            assignment = ",".join(f"CAN {channel}" for channel in assigned) or "(none)"
        lines.extend(
            [
                "",
                f"DATABASE: {path.name}",
                f"  Assignment: {assignment}",
            ]
        )
        if not path.exists():
            overall = "FAIL"
            lines.append("  LOAD FAIL file not found")
            continue
        stat = path.stat()
        lines.append(f"  Size: {stat.st_size:,} bytes")
        try:
            decoder = DBCDecoder(path)
            database = decoder.database
            messages = list(database.messages)
            signals = [
                signal
                for message in messages
                for signal in getattr(message, "signals", [])
            ]
            compatibility = any(
                "compatibility mode" in message.lower()
                for message in decoder.load_messages
            )
            strict_detail = next(
                (
                    message.split(":", 1)[-1].strip()
                    for message in decoder.load_messages
                    if message.startswith("Strict mode details:")
                ),
                "",
            )
            lines.extend(
                [
                    (
                        f"  LOAD {'WARN' if compatibility else 'PASS'} "
                        f"{'compatibility mode' if compatibility else 'strict mode'}"
                    ),
                    f"  Messages: {len(messages):,} | Signals: {len(signals):,}",
                ]
            )
            if compatibility:
                overall = "WARN" if overall == "PASS" else overall
                lines.append(
                    f"  Strict failure: {_redact(strict_detail, [path])}"
                )

            key_counts = Counter(
                (
                    int(getattr(message, "frame_id", -1)),
                    bool(getattr(message, "is_extended_frame", False)),
                )
                for message in messages
            )
            duplicates = [key for key, count in key_counts.items() if count > 1]
            invalid_lengths = [
                message
                for message in messages
                if not 0 < int(getattr(message, "length", 0) or 0) <= 64
            ]
            fd_messages = sum(
                bool(getattr(message, "is_fd", False)) for message in messages
            )
            mux_messages = sum(
                any(
                    bool(getattr(signal, "is_multiplexer", False))
                    or bool(getattr(signal, "multiplexer_ids", None))
                    for signal in getattr(message, "signals", [])
                )
                for message in messages
            )
            lines.append(
                f"  CAN-FD messages: {fd_messages:,} | "
                f"Multiplexed messages: {mux_messages:,}"
            )
            lines.append(
                f"  Duplicate ID/type definitions: {len(duplicates)} | "
                f"Invalid message lengths: {len(invalid_lengths)}"
            )
            if duplicates:
                overall = "WARN" if overall == "PASS" else overall
                lines.append(
                    "  Duplicate IDs: "
                    + ", ".join(
                        f"0x{frame_id:X}/{'EXT' if extended else 'STD'}"
                        for frame_id, extended in duplicates[:20]
                    )
                )
            for message in invalid_lengths[:10]:
                overall = "WARN" if overall == "PASS" else overall
                lines.append(
                    f"  LENGTH WARN {getattr(message, 'name', '?')} "
                    f"ID=0x{int(getattr(message, 'frame_id', 0)):X} "
                    f"length={getattr(message, 'length', '?')}"
                )

            relevant_ids: set[int] = set()
            for channel in assigned:
                relevant_ids.update(observed.get(channel, set()))
            if raw_path == fallback_path and not assigned:
                for frame_ids in observed.values():
                    relevant_ids.update(frame_ids)

            if relevant_ids:
                matched = {
                    frame_id
                    for frame_id in relevant_ids
                    if _database_candidates(decoder, frame_id)
                }
                unmatched = sorted(relevant_ids - matched)
                lines.append(
                    f"  Observed IDs: {len(relevant_ids):,} | "
                    f"Matched: {len(matched):,} | Unmatched: {len(unmatched):,}"
                )
                if unmatched:
                    overall = "WARN" if overall == "PASS" else overall
                    lines.append(
                        "  Unmatched IDs: "
                        + ", ".join(f"0x{frame_id:X}" for frame_id in unmatched[:30])
                        + (" ..." if len(unmatched) > 30 else "")
                    )
            else:
                lines.append(
                    "  Observed-ID matching: unavailable until fixed ID fields "
                    "can be pre-scanned"
                )

            if duplicates or invalid_lengths or compatibility:
                suspicious = []
                duplicate_set = set(duplicates)
                invalid_set = {id(message) for message in invalid_lengths}
                for message in messages:
                    key = (
                        int(getattr(message, "frame_id", -1)),
                        bool(getattr(message, "is_extended_frame", False)),
                    )
                    if (
                        key in duplicate_set
                        or id(message) in invalid_set
                        or compatibility
                    ):
                        suspicious.append(message)
                    if len(suspicious) >= 5:
                        break
                lines.append("  SUSPICIOUS MESSAGE DETAILS")
                for message in suspicious:
                    lines.append(
                        f"    {getattr(message, 'name', '?')} "
                        f"ID=0x{int(getattr(message, 'frame_id', 0)):X} "
                        f"len={getattr(message, 'length', '?')} "
                        f"ext={bool(getattr(message, 'is_extended_frame', False))} "
                        f"fd={bool(getattr(message, 'is_fd', False))}"
                    )
                    for signal in list(getattr(message, "signals", []))[:24]:
                        lines.append(
                            "      "
                            f"{getattr(signal, 'name', '?')} "
                            f"start={getattr(signal, 'start', '?')} "
                            f"len={getattr(signal, 'length', '?')} "
                            f"order={getattr(signal, 'byte_order', '?')} "
                            f"signed={getattr(signal, 'is_signed', '?')} "
                            f"scale={getattr(signal, 'scale', '?')} "
                            f"offset={getattr(signal, 'offset', '?')} "
                            f"unit={_value(getattr(signal, 'unit', ''))} "
                            f"mux={_value(getattr(signal, 'multiplexer_ids', None))}"
                        )
        except Exception as exc:
            overall = "FAIL"
            lines.append(f"  LOAD FAIL {_exception_text(exc, [path])}")
            formatted = _redact("".join(traceback.format_exception(exc)), [path])
            lines.extend(
                ["  TRACEBACK"] + [f"    {line}" for line in formatted.splitlines()[-12:]]
            )

    lines.extend(
        [
            "",
            f"DATABASE STATUS: {overall}",
            (
                "DLC-to-message-length validation is appended by Load + Decode "
                "when raw frame lengths become available."
            ),
        ]
    )
    return "\n".join(lines)


def format_runtime_failure(error_message: str, measurement_path: str | Path) -> str:
    """Create a compact failure classification followed by redacted evidence."""
    path = Path(measurement_path)
    redacted = _redact(error_message, [path])
    lowered = redacted.lower()
    if "wrong signal data block refence" in lowered or (
        "vlsd" in lowered and "data block" in lowered
    ):
        code = "MDF-VLSD-PAYLOAD"
        layer = "MDF variable-length CAN payload storage"
        dbc = "NO - DBC decoding was not reached"
    elif "database" in lowered or ".dbc" in lowered or ".arxml" in lowered:
        code = "DATABASE-LOAD-OR-MATCH"
        layer = "DBC/ARXML loading or matching"
        dbc = "YES"
    elif "file not found" in lowered:
        code = "INPUT-FILE-MISSING"
        layer = "Input selection"
        dbc = "UNKNOWN"
    else:
        code = "LOAD-RUNTIME-FAILURE"
        layer = "CANScope loading pipeline"
        dbc = "UNKNOWN"
    first_line = next((line.strip() for line in redacted.splitlines() if line.strip()), "")
    return "\n".join(
        [
            "",
            "=" * 100,
            "LOAD + DECODE RESULT: FAIL",
            f"CLASSIFICATION: {code}",
            f"FAILURE LAYER: {layer}",
            f"DBC INVOLVED: {dbc}",
            f"ERROR: {first_line}",
            "",
            "FULL REDACTED EXCEPTION",
            redacted,
        ]
    )
