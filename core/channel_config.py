"""
ChannelConfig — maps CAN channel numbers to DBC file paths.

This is the persistent "vehicle / project configuration" layer, kept
separate from session config (signals, plot layout, measurement path).

Saved as a standalone JSON file (.canscope_ch):

    {
        "type": "canscope_channel_config",
        "version": 1,
        "name": "Truck ECU Setup",
        "channels": {
            "1": "/path/to/Powertrain.dbc",
            "2": "/path/to/Chassis.dbc",
            "0": "/path/to/FallbackAll.dbc"   <- channel 0 = "All Channels"
        }
    }

Channel key 0 (ALL_CHANNELS_KEY) means "apply to all channels that have
no specific DBC assigned".

Usage in LoadWorker:
    channel_config.decoder_for(channel)  -> DBCDecoder | None
"""
from __future__ import annotations

import json
from pathlib import Path

# ── Sentinel: channel 0 = "All Channels" fallback ────────────────────────
ALL_CHANNELS_KEY = 0


class ChannelConfig:
    """
    Maps CAN channel numbers to DBC file paths.

    Attributes
    ----------
    name : str
        Human-readable label (e.g. "Truck ECU v2").
    channels : dict[int, str]
        ``{channel_num: dbc_absolute_path}``.
        Key ``ALL_CHANNELS_KEY`` (0) = fallback for unassigned channels.
    """

    FILE_EXTENSION = ".canscope_ch"

    def __init__(
        self,
        name: str = "Unnamed Configuration",
        channels: dict[int, str] | None = None,
    ) -> None:
        self.name = name
        # channel int → absolute dbc path string
        self.channels: dict[int, str] = dict(channels or {})

    # ── Factory methods ───────────────────────────────────────────────────

    @classmethod
    def from_single_dbc(cls, dbc_path: str) -> "ChannelConfig":
        """Compatibility helper: single-DBC workflow → apply to all channels."""
        return cls(
            name=Path(dbc_path).stem,
            channels={ALL_CHANNELS_KEY: str(dbc_path)},
        )

    @classmethod
    def load(cls, path: str | Path) -> "ChannelConfig":
        """Load from a .canscope_ch JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("type") != "canscope_channel_config":
            raise ValueError(f"Not a channel config file: {path}")
        channels = {int(k): str(v) for k, v in data.get("channels", {}).items()}
        return cls(name=str(data.get("name", "Unnamed")), channels=channels)

    def save(self, path: str | Path) -> None:
        """Save to a .canscope_ch JSON file."""
        data = {
            "type": "canscope_channel_config",
            "version": 1,
            "name": self.name,
            "channels": {str(k): v for k, v in self.channels.items()},
        }
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ── Query helpers ─────────────────────────────────────────────────────

    def dbc_path_for(self, channel: int | None) -> str | None:
        """
        Return the DBC path assigned to *channel*, or the All-Channels
        fallback, or None if neither is configured.
        """
        if channel is not None and channel in self.channels:
            return self.channels[channel]
        return self.channels.get(ALL_CHANNELS_KEY)

    def decoder_for(self, channel: int | None):
        """
        Return a warm DBCDecoder for *channel*, or None.

        Decoders are created lazily and cached — the same DBC file shared
        across channels reuses one decoder instance (saves RAM + parse time).
        """
        path = self.dbc_path_for(channel)
        if not path:
            return None
        if path not in self._decoder_cache:
            from core.dbc_decoder import DBCDecoder
            self._decoder_cache[path] = DBCDecoder(path)
        return self._decoder_cache[path]

    def build_all_decoders(self) -> dict[str, object]:
        """
        Pre-build and warm all decoders. Called before decode loop starts
        so the first frame doesn't pay the DBC parse cost.
        Returns {dbc_path: DBCDecoder}.
        """
        for path in set(self.channels.values()):
            _ = self.decoder_for(None)  # prime via path lookup
            if path not in self._decoder_cache:
                from core.dbc_decoder import DBCDecoder
                self._decoder_cache[path] = DBCDecoder(path)
        return dict(self._decoder_cache)

    def is_empty(self) -> bool:
        return not self.channels

    def all_dbc_paths(self) -> list[str]:
        """Return deduplicated list of DBC paths."""
        return list(dict.fromkeys(self.channels.values()))

    def assigned_channels(self) -> list[int]:
        """Return channel numbers (excluding ALL_CHANNELS_KEY=0)."""
        return [c for c in self.channels if c != ALL_CHANNELS_KEY]

    def summary(self) -> str:
        """Human-readable summary for the Log tab."""
        lines = [f'Channel config: "{self.name}"']
        for ch, path in sorted(self.channels.items()):
            label = f"CAN {ch}" if ch != ALL_CHANNELS_KEY else "All Channels"
            lines.append(f"  {label} → {Path(path).name}")
        return "\n".join(lines)

    # ── Internal ──────────────────────────────────────────────────────────

    @property
    def _decoder_cache(self) -> dict:
        # Stored on instance to survive across multiple decode runs in the
        # same session without re-parsing the DBC file.
        try:
            return self.__decoder_cache
        except AttributeError:
            self.__decoder_cache: dict = {}
            return self.__decoder_cache

    def invalidate_cache(self) -> None:
        """Force decoder re-creation (e.g. after DBC file changes on disk)."""
        try:
            del self.__decoder_cache
        except AttributeError:
            pass
