import sys
import math
import argparse
from collections import defaultdict
from typing import Iterable

import polars as pl
from scipy import signal
from intervaltree import Interval, IntervalTree


def get_interval(
    df: pl.DataFrame,
    interval: Interval,
    ignore_intervals: Iterable[Interval] | None = None,
) -> pl.DataFrame:
    df_res = df.filter(
        (pl.col("st") >= interval.begin) & (pl.col("end") <= interval.end)
    )
    if ignore_intervals:
        df_res = df_res.with_columns(
            ignore=pl.when(
                pl.any_horizontal(
                    (pl.col("st") >= interval.begin) & (pl.col("end") <= interval.end)
                    for interval in ignore_intervals
                )
            )
            .then(pl.lit(True))
            .otherwise(pl.lit(False))
        )
    else:
        df_res = df_res.with_columns(ignore=pl.lit(False))

    return df_res


def main():
    ap = argparse.ArgumentParser(description="CDR finder.")
    ap.add_argument(
        "-i",
        "--infile",
        default=sys.stdin,
        type=argparse.FileType("rb"),
        required=True,
        help="Average 5mC methylation signal as 4-column bedfile.",
    )
    ap.add_argument(
        "-o",
        "--outfile",
        default=sys.stdout,
        type=argparse.FileType("wt"),
        help="CDR regions as 3-column bedfile.",
    )
    ap.add_argument(
        "--bp_merge", type=int, default=None, help="Base pairs to merge CDRs."
    )
    ap.add_argument(
        "--thr_height_perc_valley",
        type=float,
        default=0.5,
        help="Threshold percent of the median methylation percentage needed as the minimal height/prominence of a valley from the median. Larger values filter for deeper valleys.",
    )
    ap.add_argument(
        "--thr_prom_perc_valley",
        type=float,
        default=None,
        help="Threshold percent of the median methylation percentage needed as the minimal prominence of a valley from the median. Larger values filter for prominent valleys.",
    )
    ap.add_argument(
        "--bp_edge",
        type=int,
        default=5_000,
        help="Bases to look on both edges of cdr to determine effective height.",
    )

    args = ap.parse_args()
    df = pl.read_csv(
        args.infile,
        separator="\t",
        has_header=False,
        new_columns=["chrom", "st", "end", "avg"],
    )

    cdr_intervals: defaultdict[str, IntervalTree] = defaultdict(IntervalTree)
    for chrom, df_chr_methyl in df.group_by(["chrom"]):
        chrom: str = chrom[0]

        # Group adjacent, contiguous intervals.
        df_chr_methyl_adj_groups = (
            df_chr_methyl.with_columns(brk=pl.col("end") == pl.col("st").shift(-1))
            .fill_null(True)
            .with_columns(pl.col("brk").rle_id())
            # Group contiguous intervals.
            .with_columns(
                pl.when(pl.col("brk") % 2 == 0)
                .then(pl.col("brk") + 1)
                .otherwise(pl.col("brk"))
            )
            .partition_by("brk")
        )
        cdr_prom_thr = (
            df_chr_methyl["avg"].median() * args.thr_prom_perc_valley
            if args.thr_prom_perc_valley
            else None
        )
        cdr_height_thr = df_chr_methyl["avg"].median() * args.thr_height_perc_valley
        print(
            f"Using CDR height threshold of {cdr_height_thr} and prominence threshold of {cdr_prom_thr} for {chrom}.",
            file=sys.stderr,
        )

        # Find peaks within the signal per group.
        for df_chr_methyl_adj_grp in df_chr_methyl_adj_groups:
            df_chr_methyl_adj_grp = df_chr_methyl_adj_grp.with_row_index()

            # Require valley has prominence of some percentage of median methyl signal.
            # Invert for peaks.
            _, peak_info = signal.find_peaks(
                -df_chr_methyl_adj_grp["avg"], width=1, prominence=cdr_prom_thr
            )

            grp_cdr_intervals: set[Interval] = set()
            for cdr_st_idx, cdr_end_idx, cdr_prom in zip(
                peak_info["left_ips"], peak_info["right_ips"], peak_info["prominences"]
            ):
                # Convert approx indices to indices
                cdr_st = df_chr_methyl_adj_grp.filter(
                    pl.col("index") == math.floor(cdr_st_idx)
                ).row(0, named=True)["st"]
                cdr_end = df_chr_methyl_adj_grp.filter(
                    pl.col("index") == math.ceil(cdr_end_idx)
                ).row(0, named=True)["end"]

                grp_cdr_intervals.add(Interval(cdr_st, cdr_end, cdr_prom))

            for interval in grp_cdr_intervals:
                cdr_st, cdr_end, cdr_prom = interval.begin, interval.end, interval.data
                ignore_intervals = grp_cdr_intervals.difference([interval])
                df_cdr = get_interval(df_chr_methyl_adj_grp, interval)

                interval_cdr_left = Interval(cdr_st - args.bp_edge, cdr_st)
                interval_cdr_right = Interval(cdr_end, cdr_end + args.bp_edge)

                # Get left and right side of CDR.
                # Subtract intervals if overlapping bp edge region.
                # Set ignored intervals on sides of CDR to average methylation median.
                # This does not affect other calls and is just to look at valley in isolation.
                df_cdr_left = get_interval(
                    df_chr_methyl_adj_grp, interval_cdr_left, ignore_intervals
                ).with_columns(
                    avg=pl.when(pl.col("ignore"))
                    .then(df_chr_methyl["avg"].median())
                    .otherwise(pl.col("avg"))
                )
                df_cdr_right = get_interval(
                    df_chr_methyl_adj_grp, interval_cdr_right, ignore_intervals
                ).with_columns(
                    avg=pl.when(pl.col("ignore"))
                    .then(df_chr_methyl["avg"].median())
                    .otherwise(pl.col("avg"))
                )

                cdr_low = df_cdr["avg"].min()
                cdr_right_median = df_cdr_right["avg"].median()
                cdr_left_median = df_cdr_left["avg"].median()
                # If empty, use median.
                cdr_edge_height = min(
                    cdr_right_median
                    if cdr_right_median
                    else df_chr_methyl_adj_grp["avg"].median(),
                    cdr_left_median
                    if cdr_left_median
                    else df_chr_methyl_adj_grp["avg"].median(),
                )

                # Calculate the height of this CDR looking at edges.
                cdr_height = cdr_edge_height - cdr_low

                # Add merge distance bp.
                if args.bp_merge:
                    cdr_st = cdr_st - args.bp_merge
                    cdr_end = cdr_end + args.bp_merge

                if cdr_height >= cdr_height_thr:
                    print(
                        f"Found CDR at {chrom}:{interval.begin}-{interval.end} with height of {cdr_height} and prominence {cdr_prom}.",
                        file=sys.stderr,
                    )
                    cdr_intervals[chrom].add(Interval(cdr_st, cdr_end))

    # Merge overlaps and output.
    for chrom, cdrs in cdr_intervals.items():
        if args.bp_merge:
            starting_intervals = len(cdrs)
            cdrs.merge_overlaps()
            print(
                f"Merged {starting_intervals - len(cdrs)} intervals in {chrom}.",
                file=sys.stderr,
            )

        for cdr in cdrs.iter():
            cdr_st, cdr_end = cdr.begin, cdr.end
            if args.bp_merge:
                cdr_st += args.bp_merge
                cdr_end -= args.bp_merge

            args.outfile.write(f"{chrom}\t{cdr_st}\t{cdr_end}\n")


if __name__ == "__main__":
    raise SystemExit(main())