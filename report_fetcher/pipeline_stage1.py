# pipeline_stage1.py
from __future__ import annotations

import logging
from functools import reduce
from typing import List, Optional

import pandas as pd
from tqdm import tqdm
from . import new_functions

from .config import SCALESERP_API_KEY  # default API key (can be overridden by arg)
from .config import (  # CITY_NAME_COLUMN,
    # ASSOCIATION_NAME_COLUMN,
    CITY_NAME_COLUMN,
    COLUMNS,
    COUNTRY_NAME_COLUMN,
    MAX_PAGE_RESULTS,
    MAX_PDF_RESULTS,
    REGION_NAME_COLUMN,
    SUBREGION_NAME_COLUMN,
    COLUMNS_IMP
)
# from .search import find_report_links_categorized, process_companies_for_reports
from .search_focused import process_companies_for_reports_focused as process_companies_for_reports

# for column in COLUMNS:
#     from .config import column

logger = logging.getLogger(__name__)


def stage1_main(
    df_input: pd.DataFrame,
    scaleserp_api_key: Optional[str] = None,
    *,
    include_country_in_search: bool = True,
    show_progress: bool = True,
    progress_offset: int = 0,
    progress_total: Optional[int] = None,
) -> pd.DataFrame:
    """
    Stage 1: run primary search and return a *clean* DataFrame of links.

    progress_offset/progress_total: scan-wide progress context (see
    utils.progress_label); forwarded into the per-entity search loop.
    """
    logger.info("--- Stage 1: Finding Report Links (Primary Search) ---")

    df_with_links = process_companies_for_reports(
        df_input,
        api_key=scaleserp_api_key,
        include_country_in_search=include_country_in_search,
        show_progress=show_progress,
        progress_offset=progress_offset,
        progress_total=progress_total,
    )



    logger.info(new_functions.print_to_logger(df_with_links.to_string()))
    # ---- Clean up to avoid "blank every 2nd row" in CSV/Excel ----
    if not df_with_links.empty:
        # 1) Trim whitespace on all object (string) columns
        obj_cols = df_with_links.select_dtypes(include=["object"]).columns
        for c in obj_cols:
            df_with_links[c] = df_with_links[c].astype(str).str.strip()

        # 2) Turn pure-whitespace strings into real empties (NA)
        df_with_links.replace(to_replace=r"^\s*$", value=pd.NA, regex=True, inplace=True)

        # 3) Drop rows that are entirely empty
        df_with_links.dropna(how="all", inplace=True)
        logger.info(new_functions.print_to_logger(df_with_links.to_string()))

        # 4) Require City & Country to be present
        # if CITY_NAME_COLUMN in df_with_links and COUNTRY_COLUMN in df_with_links:

        if not (False in [column_name in df_with_links for column_name in COLUMNS_IMP]):
            listc = [df_with_links[column_name].notna() for column_name in COLUMNS_IMP]
            logger.info(new_functions.print_to_logger(listc))

            # print(listc)
            df_with_links = df_with_links[
                # df_with_links[CITY_NAME_COLUMN].notna() &
                # df_with_links[COUNTRY_COLUMN].notna()
                reduce(
                    lambda a, b: a & b,
                    [df_with_links[column_name].notna() for column_name in COLUMNS_IMP],
                )
            ]

        # 5) Reset index so saving to CSV won’t introduce odd gaps
        df_with_links.reset_index(drop=True, inplace=True)

        logger.info(new_functions.print_to_logger(df_with_links.to_string()))


    logger.info("--- Stage 1: Finished; %d rows processed ---", len(df_with_links))
    return df_with_links
