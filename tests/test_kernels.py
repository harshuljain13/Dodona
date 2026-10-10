from __future__ import annotations

import pytest

from kernels.engineering import (
    KernelShape,
    choose_backend,
    hbm_bytes,
    pick_tile,
    report,
    sram_bytes,
    tile_fits,
)

def test_flash_beats_naive_on_long_prefill() -> None:
    shape = KernelShape(q_len=2048, kv_len=2048, paged=False)
    naive = hbm_bytes(shape, "naive")
    flash = hbm_bytes(shape, "flash_prefill")
    assert flash < 0.25 * naive
    assert choose_backend(shape) == "flash_prefill"

def test_decode_is_kv_bound_so_flash_saves_little() -> None:
    prefill = KernelShape(q_len=2048, kv_len=2048, paged=False)
    decode = KernelShape(q_len=1, kv_len=2048, paged=False)
    prefill_ratio = hbm_bytes(prefill, "naive") / hbm_bytes(prefill, "flash_prefill")
    decode_ratio = hbm_bytes(decode, "naive") / hbm_bytes(decode, "flash_decode")
    assert prefill_ratio > 4
    assert decode_ratio < 1.2
    assert choose_backend(decode) == "flash_decode"

def test_paged_adds_gather_over_flash() -> None:
    shape = KernelShape(q_len=128, kv_len=2048, paged=True)
    flash = hbm_bytes(shape, "flash_prefill")
    paged = hbm_bytes(shape, "paged_prefill")
    assert paged > flash
    assert choose_backend(shape) == "paged_prefill"

def test_pick_tile_fits_h100_sram() -> None:
    shape = KernelShape(q_len=128, kv_len=4096, head_dim=128)
    tiled = pick_tile(shape)
    assert tile_fits(tiled)
    assert tiled.tile_k >= 16
    huge = KernelShape(q_len=128, kv_len=4096, head_dim=16_384, tile_q=256, tile_k=256)
    assert sram_bytes(huge) > 227_328
    with pytest.raises(ValueError):
        pick_tile(huge, sram=1024)

def test_report_names_backend_and_ratio() -> None:
    lines = "\n".join(report(KernelShape(q_len=128, kv_len=2048)))
    assert "backend paged_prefill" in lines
    assert "naive/flash=" in lines
