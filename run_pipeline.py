# run_pipeline.py
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from pprint import pprint
from typing import Optional

import pandas as pd

# --- Load .env BEFORE importing anything that reads env vars ---
try:
    from dotenv import load_dotenv  # optional

    load_dotenv(".env")  # adjust if your env file is named differently
except Exception:
    pass

from report_fetcher.config import (  # COMPANY_NAME_COLUMN,
    # ASSOCIATION_NAME_COLUMN,
    BASE_DIR,
    CITY_NAME_COLUMN,
    COLUMNS,
    COUNTRY_NAME_COLUMN,
    OPENAI_API_KEY,
    REGION_NAME_COLUMN,
    SCALESERP_API_KEY,
    SUBREGION_NAME_COLUMN,
)

# Now it's safe to import config and pipeline modules
from report_fetcher.pipeline_stage1 import stage1_main
from report_fetcher.pipeline_stage2 import stage2_main


def _save_clean_csv(df: pd.DataFrame, path: Path, keep_cols: list[str] | None = None) -> None:
    """
    Save a CSV with:
      - trimmed string columns,
      - blank/whitespace-only cells converted to NA,
      - all-empty rows dropped,
      - reset index,
      - single LF line terminator to avoid 'blank every 2nd row' on Windows.
    """
    if df is None:
        pd.DataFrame().to_csv(path, index=False, lineterminator="\n", encoding="utf-8")
        return

    out = df.copy()

    # Keep only selected columns (if provided and present)
    if keep_cols:
        keep = [c for c in keep_cols if c in out.columns]
        out = out[keep]

    # Strip whitespace in object columns
    obj_cols = out.select_dtypes(include=["object"]).columns
    for c in obj_cols:
        out[c] = out[c].astype(str).str.strip()

    # Convert pure whitespace to NA
    out.replace(to_replace=r"^\s*$", value=pd.NA, regex=True, inplace=True)

    # Drop all-empty rows and reset index
    out.dropna(how="all", inplace=True)
    out.reset_index(drop=True, inplace=True)

    # Use single LF to prevent blank spacer rows on Windows viewers
    out.to_csv(path, index=False, lineterminator="\n", encoding="utf-8")


def setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        # format="%(asctime)s %(levelname)s %(name)s %(funcName)s %(lineno)d: %(message)s",
        format="%(asctime)s %(levelname)s %(pathname)s, line %(lineno)d, in %(funcName)s: %(message)s",
    )


# raise
def load_input(csv_path: Optional[str]) -> pd.DataFrame:
    if csv_path:
        p = Path(csv_path)
        if not p.exists():
            raise FileNotFoundError(f"Input file not found: {p}")
        return pd.read_csv(p)
    else:
        raise ValueError("No input CSV provided.")

    # Fallback tiny sample if no CSV given
    # return pd.DataFrame(
    #     [
    #         {COMPANY_NAME_COLUMN: "ABB",        COUNTRY_COLUMN: "Switzerland"},
    #         {COMPANY_NAME_COLUMN: "Telefónica", COUNTRY_COLUMN: "Spain"},
    #     ]
    # )


def save_outputs(
    df_links: pd.DataFrame,
    results_df: pd.DataFrame,
    failed_df: pd.DataFrame,
    mismatch_df: pd.DataFrame,
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1 links: keep everything
    _save_clean_csv(df_links, out_dir / "stage1_links.csv")

    # Stage 2 outputs: (you can pass keep_cols to trim columns if desired)
    _save_clean_csv(results_df, out_dir / "stage2_results.csv")
    _save_clean_csv(failed_df, out_dir / "stage2_failed_downloads.csv")
    _save_clean_csv(mismatch_df, out_dir / "stage2_type_mismatch.csv")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run report-fetcher pipeline: Stage 1 (search) -> Stage 2 (download & classify)."
    )
    parser.add_argument(
        "--input-csv",
        # help=f"Path to CSV with columns '{COMPANY_NAME_COLUMN}' and '{COUNTRY_COLUMN}'.",
        help=f"Path to CSV with columns" + " ".join(COLUMNS),
    )
    parser.add_argument(
        "--openai-api-key",
        help="Override OPENAI_API_KEY (otherwise taken from environment/config).",
    )
    parser.add_argument(
        "--scaleserp-api-key",
        help="Override SCALESERP_API_KEY (otherwise taken from environment/config).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=1,
        help="Increase verbosity (use -v for INFO, -vv for DEBUG).",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Load data
    df = load_input(args.input_csv)
    # df.fillna("", inplace=True)
    logging.info("Loaded %d companies. Columns: %s", len(df), list(df.columns))

    # Resolve API keys (CLI > env/config)
    openai_key = args.openai_api_key or OPENAI_API_KEY
    scaleserp_key = args.scaleserp_api_key or SCALESERP_API_KEY

    if not scaleserp_key:
        raise RuntimeError(
            "SCALESERP_API_KEY is missing. Set it via --scaleserp-api-key or environment variable."
        )
    if not openai_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Set it via --openai-api-key or environment variable."
        )

    # Stage 1
    df_links = stage1_main(
        df_input=df,
        scaleserp_api_key=scaleserp_key,
        include_country_in_search=True,
        show_progress=True,
    )

    pprint(df_links)
    # exit()
    # Stage 2
    results_df, failed_df, mismatch_df = stage2_main(
        df_with_links=df_links,
        openai_api_key=openai_key,
        scaleserp_api_key=scaleserp_key,
        show_progress=True,
    )

    # Save artifacts under outputs/
    out_dir = BASE_DIR  # defined in config.py (outputs/)
    save_outputs(df_links, results_df, failed_df, mismatch_df, out_dir)

    logging.info("Done. Outputs written to %s", out_dir.resolve())


if __name__ == "__main__":
    main()
