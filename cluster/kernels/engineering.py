from __future__ import annotations

import argparse
from dataclasses import dataclass

FP16 = 2
H100_SRAM_BYTES = 227_328

@dataclass(frozen=True)
class KernelShape:
    batch: int = 1
    heads: int = 32
    q_len: int = 128
    kv_len: int = 2048
    head_dim: int = 128
    block_size: int = 16
    tile_q: int = 64
    tile_k: int = 64
    paged: bool = True

def _qkv_bytes(shape: KernelShape) -> int:
    q = shape.batch * shape.heads * shape.q_len * shape.head_dim * FP16
    kv = shape.batch * shape.heads * shape.kv_len * shape.head_dim * FP16
    o = q
    return q + 2 * kv + o

def _score_bytes(shape: KernelShape) -> int:
    return shape.batch * shape.heads * shape.q_len * shape.kv_len * FP16

def hbm_bytes(shape: KernelShape, backend: str) -> int:
    qkvo = _qkv_bytes(shape)
    score = _score_bytes(shape)
    if backend == "naive":
        return qkvo + 2 * score
    n_blocks = (shape.kv_len + shape.block_size - 1) // shape.block_size
    gather = n_blocks * shape.heads * shape.head_dim * FP16 if shape.paged else 0
    if backend in ("flash_prefill", "flash_decode"):
        return qkvo
    if backend in ("paged_prefill", "paged_decode"):
        return qkvo + gather
    raise ValueError(f"unknown backend {backend}")

def sram_bytes(shape: KernelShape) -> int:
    q = shape.tile_q * shape.head_dim * FP16
    k = shape.tile_k * shape.head_dim * FP16
    v = k
    acc = shape.tile_q * shape.head_dim * 4
    return q + k + v + acc

def tile_fits(shape: KernelShape, sram: int = H100_SRAM_BYTES) -> bool:
    return sram_bytes(shape) <= sram

def pick_tile(
    shape: KernelShape,
    *,
    sram: int = H100_SRAM_BYTES,
) -> KernelShape:
    tile_q = 64 if shape.q_len >= 64 else max(1, shape.q_len)
    for tile_k in (128, 64, 32, 16):
        candidate = KernelShape(
            batch=shape.batch,
            heads=shape.heads,
            q_len=shape.q_len,
            kv_len=shape.kv_len,
            head_dim=shape.head_dim,
            block_size=shape.block_size,
            tile_q=tile_q,
            tile_k=min(tile_k, shape.kv_len),
            paged=shape.paged,
        )
        if tile_fits(candidate, sram):
            return candidate
    raise ValueError("no tile fits SRAM")

def choose_backend(shape: KernelShape) -> str:
    if shape.q_len <= 1:
        return "paged_decode" if shape.paged else "flash_decode"
    if shape.paged:
        return "paged_prefill"
    return "flash_prefill"

def report(shape: KernelShape) -> list[str]:
    chosen = choose_backend(shape)
    tiled = pick_tile(shape)
    naive = hbm_bytes(shape, "naive")
    flash = hbm_bytes(shape, "flash_prefill" if shape.q_len > 1 else "flash_decode")
    picked = hbm_bytes(shape, chosen)
    ratio = naive / flash if flash else 0.0
    return [
        f"shape q={shape.q_len} kv={shape.kv_len} d={shape.head_dim} "
        f"h={shape.heads} paged={int(shape.paged)}",
        f"backend {chosen}  tile_q={tiled.tile_q} tile_k={tiled.tile_k} "
        f"sram={sram_bytes(tiled)}",
        f"hbm naive={naive} flash={flash} chosen={picked}  naive/flash={ratio:.2f}",
    ]

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Class 10 attention kernel cost model")
    p.add_argument("--q", type=int, default=128)
    p.add_argument("--kv", type=int, default=2048)
    p.add_argument("--heads", type=int, default=32)
    p.add_argument("--dim", type=int, default=128)
    p.add_argument("--contiguous", action="store_true")
    args = p.parse_args(argv)
    shape = KernelShape(
        q_len=args.q,
        kv_len=args.kv,
        heads=args.heads,
        head_dim=args.dim,
        paged=not args.contiguous,
    )
    for line in report(shape):
        print(line)

if __name__ == "__main__":
    main()
