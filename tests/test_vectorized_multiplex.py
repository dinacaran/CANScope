from types import SimpleNamespace

import numpy as np

from core.vectorized_decoder import MessageVectorDecoder


def _signal(name, start, *, is_mux=False, mux_ids=None, mux_signal=None):
    return SimpleNamespace(
        name=name,
        unit="",
        choices=None,
        scale=1.0,
        offset=0.0,
        start=start,
        length=8,
        byte_order="little_endian",
        is_signed=False,
        is_float=False,
        is_multiplexer=is_mux,
        multiplexer_ids=mux_ids,
        multiplexer_signal=mux_signal,
    )


class _MuxMessage:
    length = 3
    signals = [
        _signal("Mux", 0, is_mux=True),
        _signal("BranchA", 8, mux_ids=[1], mux_signal="Mux"),
        _signal("BranchB", 16, mux_ids=[2], mux_signal="Mux"),
    ]

    def decode(self, payload, **_kwargs):
        mux = payload[0]
        result = {"Mux": mux}
        if mux == 1:
            result["BranchA"] = payload[1]
        elif mux == 2:
            result["BranchB"] = payload[2]
        return result


def test_simple_multiplexing_is_vectorized_and_sparse():
    decoder = MessageVectorDecoder(_MuxMessage())
    data = np.array([
        [1, 10, 99],
        [2, 88, 20],
        [1, 30, 77],
    ], dtype=np.uint8)

    result = decoder.decode(data)

    assert decoder.fully_fast is True
    np.testing.assert_allclose(result["Mux"][1], [1, 2, 1])
    np.testing.assert_allclose(result["BranchA"][1], [10, np.nan, 30], equal_nan=True)
    np.testing.assert_allclose(result["BranchB"][1], [np.nan, 20, np.nan], equal_nan=True)
