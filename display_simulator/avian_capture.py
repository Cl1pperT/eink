"""Subprocess helper for an offline collage rendered by AvianVisitors itself."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


DEMO_SPECIES = (
    {"sci": "Poecile gambeli", "com": "Mountain Chickadee", "n": 42, "rarity_weight": 1.0},
    {"sci": "Haemorhous mexicanus", "com": "House Finch", "n": 31, "rarity_weight": 1.0},
    {"sci": "Zenaida macroura", "com": "Mourning Dove", "n": 24, "rarity_weight": 1.0},
    {"sci": "Spinus tristis", "com": "American Goldfinch", "n": 17, "rarity_weight": 2.0},
    {"sci": "Corvus corax", "com": "Common Raven", "n": 11, "rarity_weight": 2.5},
    {"sci": "Sitta pygmaea", "com": "Pygmy Nuthatch", "n": 8, "rarity_weight": 5.0},
    {"sci": "Icterus bullockii", "com": "Bullock's Oriole", "n": 5, "rarity_weight": 7.0},
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--mat", type=float, default=0.04)
    parser.add_argument("--collage-vh", type=float, default=52)
    parser.add_argument("--cluster-xbias", type=float, default=1.0)
    parser.add_argument("--cluster-ybias", type=float, default=1.2)
    parser.add_argument("--count-exp", type=float, default=1.0)
    parser.add_argument("--cluster-pad", type=int, default=1)
    parser.add_argument("--packing-budget", type=float)
    parser.add_argument("--window-hours", type=int, default=168)
    args = parser.parse_args(argv)
    frame = args.repo.resolve() / "frame"
    if not (frame / "shoot.py").is_file():
        print(f"AvianVisitors frame/shoot.py not found under {args.repo}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(frame))
    try:
        from shoot import shoot_birdweather
        shoot_birdweather(
            args.out,
            list(DEMO_SPECIES),
            title="Avian Visitors",
            subtitle="Heard This Week",
            vw=args.width,
            vh=args.height,
            dsf=2,
            mat=args.mat,
            collage_vh=args.collage_vh,
            cluster_xbias=args.cluster_xbias,
            cluster_ybias=args.cluster_ybias,
            count_exp=args.count_exp,
            cluster_pad=args.cluster_pad,
            packing_budget=args.packing_budget,
            window_hours=args.window_hours,
            timeout_ms=30_000,
        )
    except Exception as exc:
        print(f"local AvianVisitors render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
