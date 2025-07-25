__all__ = ['ApiError', 'NixtlaClient']

import datetime
from enum import Enum
import logging
import math
import os
import re
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Literal,
    Optional,
    TypeVar,
    Union,
    overload,
)

import annotated_types
import httpcore
import httpx
import numpy as np
import orjson
import pandas as pd
import utilsforecast.processing as ufp
import zstandard as zstd
from pydantic import BaseModel
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    wait_fixed,
)
from utilsforecast.compat import DFType, DataFrame, pl_DataFrame
from utilsforecast.feature_engineering import _add_time_features, time_features
from utilsforecast.preprocessing import fill_gaps, id_time_grid
from utilsforecast.validation import ensure_time_dtype, validate_format
from utilsforecast.processing import ensure_sorted

if TYPE_CHECKING:
    try:
        from fugue import AnyDataFrame
    except ModuleNotFoundError:
        pass
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        pass
    try:
        import plotly
    except ModuleNotFoundError:
        pass
    try:
        import triad
    except ModuleNotFoundError:
        pass
    try:
        from polars import DataFrame as PolarsDataFrame
    except ModuleNotFoundError:
        pass
    try:
        from dask.dataframe import DataFrame as DaskDataFrame
    except ModuleNotFoundError:
        pass
    try:
        from pyspark.sql import DataFrame as SparkDataFrame
    except ModuleNotFoundError:
        pass
    try:
        from ray.data import Dataset as RayDataset
    except ModuleNotFoundError:
        pass

AnyDFType = TypeVar(
    "AnyDFType",
    "DaskDataFrame",
    pd.DataFrame,
    "PolarsDataFrame",
    "RayDataset",
    "SparkDataFrame",
)
DistributedDFType = TypeVar(
    "DistributedDFType",
    "DaskDataFrame",
    "RayDataset",
    "SparkDataFrame",
)
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

_PositiveInt = Annotated[int, annotated_types.Gt(0)]
_NonNegativeInt = Annotated[int, annotated_types.Ge(0)]
_Loss = Literal["default", "mae", "mse", "rmse", "mape", "smape"]
_Model = Literal["azureai", "timegpt-1", "timegpt-1-long-horizon"]
_FinetuneDepth = Literal[1, 2, 3, 4, 5]
_Freq = Union[str, int, pd.offsets.BaseOffset]
_FreqType = TypeVar("_FreqType", str, int, pd.offsets.BaseOffset)
_ThresholdMethod = Literal["univariate", "multivariate"]


class FinetunedModel(BaseModel, extra="allow"):  # type: ignore
    id: str
    created_at: datetime.datetime
    created_by: str
    base_model_id: str
    steps: int
    depth: int
    loss: _Loss
    model: _Model
    freq: str


_date_features_by_freq = {
    # Daily frequencies
    "B": ["year", "month", "day", "weekday"],
    "C": ["year", "month", "day", "weekday"],
    "D": ["year", "month", "day", "weekday"],
    # Weekly
    "W": ["year", "week", "weekday"],
    # Monthly
    "M": ["year", "month"],
    "SM": ["year", "month", "day"],
    "BM": ["year", "month"],
    "CBM": ["year", "month"],
    "MS": ["year", "month"],
    "SMS": ["year", "month", "day"],
    "BMS": ["year", "month"],
    "CBMS": ["year", "month"],
    # Quarterly
    "Q": ["year", "quarter"],
    "BQ": ["year", "quarter"],
    "QS": ["year", "quarter"],
    "BQS": ["year", "quarter"],
    # Yearly
    "A": ["year"],
    "Y": ["year"],
    "BA": ["year"],
    "BY": ["year"],
    "AS": ["year"],
    "YS": ["year"],
    "BAS": ["year"],
    "BYS": ["year"],
    # Hourly
    "BH": ["year", "month", "day", "hour", "weekday"],
    "H": ["year", "month", "day", "hour"],
    # Minutely
    "T": ["year", "month", "day", "hour", "minute"],
    "min": ["year", "month", "day", "hour", "minute"],
    # Secondly
    "S": ["year", "month", "day", "hour", "minute", "second"],
    # Milliseconds
    "L": ["year", "month", "day", "hour", "minute", "second", "millisecond"],
    "ms": ["year", "month", "day", "hour", "minute", "second", "millisecond"],
    # Microseconds
    "U": ["year", "month", "day", "hour", "minute", "second", "microsecond"],
    "us": ["year", "month", "day", "hour", "minute", "second", "microsecond"],
    # Nanoseconds
    "N": [],
}


def _retry_strategy(max_retries: int, retry_interval: int, max_wait_time: int):
    def should_retry(exc: Exception) -> bool:
        retriable_exceptions = (
            ConnectionResetError,
            httpcore.ConnectError,
            httpcore.RemoteProtocolError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.PoolTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
        )
        retriable_codes = [408, 409, 429, 502, 503, 504]
        return isinstance(exc, retriable_exceptions) or (
            isinstance(exc, ApiError) and exc.status_code in retriable_codes
        )

    def after_retry(retry_state: RetryCallState) -> None:
        error = retry_state.outcome.exception()
        logger.error(f"Attempt {retry_state.attempt_number} failed with error: {error}")

    return retry(
        retry=retry_if_exception(should_retry),
        wait=wait_fixed(retry_interval),
        after=after_retry,
        stop=stop_after_attempt(max_retries) | stop_after_delay(max_wait_time),
        reraise=True,
    )


def _maybe_infer_freq(
    df: DataFrame,
    freq: Optional[_FreqType],
    id_col: str,
    time_col: str,
) -> _FreqType:
    if freq is not None:
        return freq
    if isinstance(df, pl_DataFrame):
        raise ValueError(
            "Cannot infer frequency for a polars DataFrame, please set the "
            "`freq` argument to a valid polars offset.\nYou can find them at "
            "https://pola-rs.github.io/polars/py-polars/html/reference/expressions/api/polars.Expr.dt.offset_by.html"
        )
    assert isinstance(df, pd.DataFrame)
    sizes = df[id_col].value_counts(sort=True)
    times = df.loc[df[id_col] == sizes.index[0], time_col].sort_values()
    if times.dt.tz is not None:
        times = times.dt.tz_convert("UTC").dt.tz_localize(None)
    inferred_freq = pd.infer_freq(times.values)
    if inferred_freq is None:
        raise RuntimeError(
            "Could not infer the frequency of the time column. This could be due "
            "to inconsistent intervals. Please check your data for missing, "
            "duplicated or irregular timestamps"
        )
    logger.info(f"Inferred freq: {inferred_freq}")
    return inferred_freq


def _standardize_freq(freq: _Freq, processed: ufp.ProcessedDF) -> str:
    if isinstance(freq, str):
        # polars uses 'mo' for months, all other strings are compatible with pandas
        freq = freq.replace("mo", "MS")
    elif isinstance(freq, pd.offsets.BaseOffset):
        freq = freq.freqstr
    elif isinstance(freq, int):
        freq = "MS"
    else:
        raise ValueError(
            f"`freq` must be a string, int or pandas offset, got {type(freq).__name__}"
        )
    return freq


def _array_tails(
    x: np.ndarray,
    indptr: np.ndarray,
    out_sizes: np.ndarray,
) -> np.ndarray:
    if (out_sizes > np.diff(indptr)).any():
        raise ValueError("out_sizes must be at most the original sizes.")
    idxs = np.hstack(
        [np.arange(end - size, end) for end, size in zip(indptr[1:], out_sizes)]
    )
    return x[idxs]


def _tail(proc: ufp.ProcessedDF, n: int) -> ufp.ProcessedDF:
    new_sizes = np.minimum(np.diff(proc.indptr), n)
    new_indptr = np.append(0, new_sizes.cumsum())
    new_data = _array_tails(proc.data, proc.indptr, new_sizes)
    return ufp.ProcessedDF(
        uids=proc.uids,
        last_times=proc.last_times,
        data=new_data,
        indptr=new_indptr,
        sort_idxs=None,
    )


def _partition_series(
    payload: dict[str, Any], n_part: int, h: int
) -> list[dict[str, Any]]:
    parts = []
    series = payload.pop("series")
    n_series = len(series["sizes"])
    n_part = min(n_part, n_series)
    series_per_part = math.ceil(n_series / n_part)
    prev_size = 0
    for i in range(0, n_series, series_per_part):
        sizes = series["sizes"][i : i + series_per_part]
        curr_size = sum(sizes)
        part_idxs = slice(prev_size, prev_size + curr_size)
        prev_size += curr_size
        part_series = {
            "y": series["y"][part_idxs],
            "sizes": sizes,
        }
        if series["X"] is None:
            part_series["X"] = None
            if h > 0:
                part_series["X_future"] = None
        else:
            part_series["X"] = [x[part_idxs] for x in series["X"]]
            if h > 0:
                part_series["X_future"] = [
                    x[i * h : (i + series_per_part) * h] for x in series["X_future"]
                ]
        parts.append({"series": part_series, **payload})
    return parts


def _maybe_add_date_features(
    df: DFType,
    X_df: Optional[DFType],
    features: Union[bool, Sequence[Union[str, Callable]]],
    one_hot: Union[bool, list[str]],
    freq: _Freq,
    h: int,
    id_col: str,
    time_col: str,
    target_col: str,
) -> tuple[DFType, Optional[DFType]]:
    if not features or not isinstance(freq, str):
        return df, X_df
    if isinstance(features, list):
        date_features: Sequence[Union[str, Callable]] = features
    else:
        date_features = _date_features_by_freq.get(freq, [])
        if not date_features:
            warnings.warn(
                f"Non default date features for {freq} "
                "please provide a list of date features"
            )
    # add features
    if X_df is None:
        df, X_df = time_features(
            df=df,
            freq=freq,
            features=date_features,
            h=h,
            id_col=id_col,
            time_col=time_col,
        )
    else:
        df = _add_time_features(df, features=date_features, time_col=time_col)
        X_df = _add_time_features(X_df, features=date_features, time_col=time_col)
    # one hot
    if isinstance(one_hot, list):
        features_one_hot = one_hot
    elif one_hot:
        features_one_hot = [f for f in date_features if not callable(f)]
    else:
        features_one_hot = []
    if features_one_hot:
        X_df = ufp.assign_columns(X_df, target_col, 0)
        full_df = ufp.vertical_concat([df, X_df])
        if isinstance(full_df, pd.DataFrame):
            full_df = pd.get_dummies(full_df, columns=features_one_hot, dtype="float32")
        else:
            full_df = full_df.to_dummies(columns=features_one_hot)
        df = ufp.take_rows(full_df, slice(0, df.shape[0]))
        X_df = ufp.take_rows(full_df, slice(df.shape[0], full_df.shape[0]))
        X_df = ufp.drop_columns(X_df, target_col)
        X_df = ufp.drop_index_if_pandas(X_df)
    if h == 0:
        # time_features returns an empty df, we use it as None here
        X_df = None
    return df, X_df


def _validate_exog(
    df: DFType,
    X_df: Optional[DFType],
    id_col: str,
    time_col: str,
    target_col: str,
    hist_exog: Optional[list[str]],
) -> tuple[DFType, Optional[DFType]]:
    base_cols = {id_col, time_col, target_col}
    exogs = [c for c in df.columns if c not in base_cols]
    if hist_exog is None:
        hist_exog = []
    if X_df is None:
        # all exogs must be historic
        ignored_exogs = [c for c in exogs if c not in hist_exog]
        if ignored_exogs:
            warnings.warn(
                f"`df` contains the following exogenous features: {ignored_exogs}, "
                "but `X_df` was not provided and they were not declared in `hist_exog_list`. "
                "They will be ignored."
            )
        exogs = [c for c in exogs if c in hist_exog]
        df = df[[id_col, time_col, target_col, *exogs]]
        return df, None

    # exogs in df that weren't declared as historic nor future
    futr_exog = [c for c in X_df.columns if c not in base_cols]
    declared_exogs = {*hist_exog, *futr_exog}
    ignored_exogs = [c for c in exogs if c not in declared_exogs]
    if ignored_exogs:
        warnings.warn(
            f"`df` contains the following exogenous features: {ignored_exogs}, "
            "but they were not found in `X_df` nor declared in `hist_exog_list`. "
            "They will be ignored."
        )

    # future exogenous are provided in X_df that are not in df
    missing_futr = set(futr_exog) - set(exogs)
    if missing_futr:
        raise ValueError(
            "The following exogenous features are present in `X_df` "
            f"but not in `df`: {missing_futr}."
        )

    # features are provided through X_df but declared as historic
    futr_and_hist = set(futr_exog) & set(hist_exog)
    if futr_and_hist:
        warnings.warn(
            "The following features were declared as historic but found in `X_df`: "
            f"{futr_and_hist}, they will be considered as historic."
        )
        futr_exog = [f for f in futr_exog if f not in hist_exog]

    # Make sure df and X_df are in right order
    df = df[[id_col, time_col, target_col, *futr_exog, *hist_exog]]
    X_df = X_df[[id_col, time_col, *futr_exog]]

    return df, X_df


def _validate_input_size(
    processed: ufp.ProcessedDF,
    model_input_size: int,
    model_horizon: int,
) -> None:
    min_size = np.diff(processed.indptr).min().item()
    if min_size < model_input_size + model_horizon:
        raise ValueError(
            "Some series are too short. "
            "Please make sure that each series contains "
            f"at least {model_input_size + model_horizon} observations."
        )


def _prepare_level_and_quantiles(
    level: Optional[list[Union[int, float]]],
    quantiles: Optional[list[float]],
) -> tuple[Optional[list[Union[int, float]]], Optional[list[float]]]:
    if level is not None and quantiles is not None:
        raise ValueError("You should provide `level` or `quantiles`, but not both.")
    if quantiles is None:
        return level, quantiles
    # we recover level from quantiles
    if not all(0 < q < 1 for q in quantiles):
        raise ValueError("`quantiles` should be floats between 0 and 1.")
    level = [abs(int(100 - 200 * q)) for q in quantiles]
    return level, quantiles


def _maybe_convert_level_to_quantiles(
    df: DFType,
    quantiles: Optional[list[float]],
) -> DFType:
    if quantiles is None:
        return df
    out_cols = [c for c in df.columns if "-lo-" not in c and "-hi-" not in c]
    df = ufp.copy_if_pandas(df, deep=False)
    for q in sorted(quantiles):
        if q == 0.5:
            col = "TimeGPT"
        else:
            lv = int(100 - 200 * q)
            hi_or_lo = "lo" if lv > 0 else "hi"
            lv = abs(lv)
            col = f"TimeGPT-{hi_or_lo}-{lv}"
        q_col = f"TimeGPT-q-{int(q * 100)}"
        df = ufp.assign_columns(df, q_col, df[col])
        out_cols.append(q_col)
    return df[out_cols]


def _preprocess(
    df: DFType,
    X_df: Optional[DFType],
    h: int,
    freq: str,
    date_features: Union[bool, Sequence[Union[str, Callable]]],
    date_features_to_one_hot: Union[bool, list[str]],
    id_col: str,
    time_col: str,
    target_col: str,
) -> tuple[ufp.ProcessedDF, Optional[DFType], list[str], Optional[list[str]]]:
    df, X_df = _maybe_add_date_features(
        df=df,
        X_df=X_df,
        features=date_features,
        one_hot=date_features_to_one_hot,
        freq=freq,
        h=h,
        id_col=id_col,
        time_col=time_col,
        target_col=target_col,
    )
    processed = ufp.process_df(
        df=df, id_col=id_col, time_col=time_col, target_col=target_col
    )
    if X_df is not None and X_df.shape[1] > 2:
        X_df = ensure_time_dtype(X_df, time_col=time_col)
        processed_X = ufp.process_df(
            df=X_df,
            id_col=id_col,
            time_col=time_col,
            target_col=None,
        )
        X_future = processed_X.data.T
        futr_cols = [c for c in X_df.columns if c not in (id_col, time_col)]
    else:
        X_future = None
        futr_cols = None
    x_cols = [c for c in df.columns if c not in (id_col, time_col, target_col)]
    return processed, X_future, x_cols, futr_cols


def _forecast_payload_to_in_sample(payload):
    in_sample_payload = {
        k: v
        for k, v in payload.items()
        if k not in ("h", "finetune_steps", "finetune_loss")
    }
    del in_sample_payload["series"]["X_future"]
    return in_sample_payload


def _maybe_add_intervals(
    df: DFType,
    intervals: Optional[dict[str, list[float]]],
) -> DFType:
    if intervals is None:
        return df
    first_key = next(iter(intervals), None)
    if first_key is None or intervals[first_key] is None:
        return df
    intervals_df = type(df)(
        {f"TimeGPT-{k}": intervals[k] for k in sorted(intervals.keys())}
    )
    return ufp.horizontal_concat([df, intervals_df])


def _maybe_drop_id(df: DFType, id_col: str, drop: bool) -> DFType:
    if drop:
        df = ufp.drop_columns(df, id_col)
    return df


def _parse_in_sample_output(
    in_sample_output: dict[str, Union[list[float], dict[str, list[float]]]],
    df: DataFrame,
    processed: ufp.ProcessedDF,
    id_col: str,
    time_col: str,
    target_col: str,
) -> DataFrame:
    times = df[time_col].to_numpy()
    targets = df[target_col].to_numpy()
    if processed.sort_idxs is not None:
        times = times[processed.sort_idxs]
        targets = targets[processed.sort_idxs]
    times = _array_tails(times, processed.indptr, in_sample_output["sizes"])
    targets = _array_tails(targets, processed.indptr, in_sample_output["sizes"])
    uids = ufp.repeat(processed.uids, in_sample_output["sizes"])
    out = type(df)(
        {
            id_col: uids,
            time_col: times,
            target_col: targets,
            "TimeGPT": in_sample_output["mean"],
        }
    )
    return _maybe_add_intervals(out, in_sample_output["intervals"])  # type: ignore


def _restrict_input_samples(level, input_size, model_horizon, h) -> int:
    if level is not None:
        # add sufficient info to compute
        # conformal interval
        # @AzulGarza
        #  this is an old opinionated decision
        #  about reducing the data sent to the api
        #  to reduce latency when
        #  a user passes level. since currently the model
        #  uses conformal prediction, we can change a minimum
        #  amount of data if the series are too large
        new_input_size = 3 * input_size + max(model_horizon, h)
    else:
        # we only want to forecast
        new_input_size = input_size
    return new_input_size


def _extract_target_array(df: DataFrame, target_col: str) -> np.ndarray:
    # in pandas<2.2 to_numpy can lead to an object array if
    # the type is a pandas nullable type, e.g. pd.Float64Dtype
    # we thus use the dtype's type as the target dtype
    if isinstance(df, pd.DataFrame):
        target_dtype = df.dtypes[target_col].type
        targets = df[target_col].to_numpy(dtype=target_dtype)
    else:
        targets = df[target_col].to_numpy()
    return targets


def _process_exog_features(
    processed_data: np.ndarray,
    x_cols: list[str],
    hist_exog_list: Optional[list[str]] = None,
) -> tuple[Optional[np.ndarray], Optional[list[int]]]:
    X = None
    hist_exog = None
    if processed_data.shape[1] > 1:
        X = processed_data[:, 1:].T
        if hist_exog_list is None:
            futr_exog = x_cols
        else:
            missing_hist: set[str] = set(hist_exog_list) - set(x_cols)
            if missing_hist:
                raise ValueError(
                    "The following exogenous features were declared as historic "
                    f"but were not found in `df`: {missing_hist}."
                )
            futr_exog = [c for c in x_cols if c not in hist_exog_list]
            # match the forecast method order [future, historic]
            fcst_features_order = futr_exog + hist_exog_list
            x_idxs = [x_cols.index(c) for c in fcst_features_order]
            X = X[x_idxs]
            hist_exog = [fcst_features_order.index(c) for c in hist_exog_list]
        if futr_exog and logger:
            logger.info(f"Using future exogenous features: {futr_exog}")
        if hist_exog_list and logger:
            logger.info(f"Using historical exogenous features: {hist_exog_list}")

    return X, hist_exog


def _model_in_list(model: str, model_list: tuple[Any]) -> bool:
    for m in model_list:
        if isinstance(m, str):
            if m == model:
                return True
        elif isinstance(m, re.Pattern):
            if m.fullmatch(model):
                return True
    return False

class AuditDataSeverity(Enum):
    """Enum class to indicate audit data severity levels"""

    FAIL = "Fail"  # Indicates a critical issue that requires immediate attention
    CASE_SPECIFIC = "Case Specific"  # Indicates an issue that may be acceptable in specific contexts
    PASS = "Pass"  # Indicates that the data is acceptable

def _audit_duplicate_rows(
    df: AnyDFType,
    id_col: str = "unique_id",
    time_col: str = "ds",
) -> tuple[AuditDataSeverity, AnyDFType]:
    if isinstance(df, pd.DataFrame):
        duplicates = df.duplicated(subset=[id_col, time_col], keep=False)
        if duplicates.any():
            return AuditDataSeverity.FAIL, df[duplicates]
        return AuditDataSeverity.PASS, pd.DataFrame()
    else:
        raise ValueError(f"Dataframe type {type(df)} is not supported yet.")

def _audit_missing_dates(
    df: AnyDFType,
    freq: _Freq,
    id_col: str = "unique_id",
    time_col: str = "ds",
    start: Union[str, int, datetime.date, datetime.datetime] = "per_serie",
    end: Union[str, int, datetime.date, datetime.datetime] = "global",
) -> tuple[AuditDataSeverity, AnyDFType]:
    if isinstance(df, pd.DataFrame):
        # Fill gaps in data
        # Convert time_col to datetime if it's string/object type
        df = ensure_time_dtype(df, time_col=time_col)
        df_complete = fill_gaps(
            df, freq=freq, id_col=id_col, time_col=time_col, start=start, end=end
        )

        # Find missing dates by comparing df_complete with df
        df_missing = pd.merge(
            df_complete, df, on=[id_col, time_col], how="outer", indicator=True
        )
        df_missing = df_missing.query("_merge == 'left_only'")[[id_col, time_col]]
        if len(df_missing) > 0:
            return AuditDataSeverity.FAIL, df_missing
        return AuditDataSeverity.PASS, pd.DataFrame()
    else:
        raise ValueError(f"Dataframe type {type(df)} is not supported yet.")

def _audit_categorical_variables(
    df: AnyDFType,
    id_col: str = "unique_id",
    time_col: str = "ds",
) -> tuple[AuditDataSeverity, AnyDFType]:
    if isinstance(df, pd.DataFrame):
        # Check categorical variables in df except id_col and time_col
        categorical_cols = (
            df.select_dtypes(include=["category", "object"])
            .columns.drop([id_col, time_col], errors="ignore")
            .tolist()
        )

        if categorical_cols:
            return AuditDataSeverity.FAIL, df[categorical_cols]
        return AuditDataSeverity.PASS, pd.DataFrame()
    else:
        raise ValueError(f"Dataframe type {type(df)} is not supported yet.")

def _audit_leading_zeros(
    df: pd.DataFrame,
    id_col: str = "unique_id",
    time_col: str = "ds",
    target_col: str = "y",
) -> tuple[AuditDataSeverity, pd.DataFrame]:
    df = ensure_sorted(df, id_col=id_col, time_col=time_col)
    if isinstance(df, pd.DataFrame):
        group_info = df.groupby(id_col).agg(
            first_index=(target_col, lambda s: s.index[0]),
            first_nonzero_index=(
                target_col,
                lambda s: s.ne(0).idxmax() if s.ne(0).any() else s.index[0],
            ),
        )
        leading_zeros_df = group_info[
            group_info["first_index"] != group_info["first_nonzero_index"]
        ].reset_index()
        if len(leading_zeros_df) > 0:
            return AuditDataSeverity.CASE_SPECIFIC, leading_zeros_df
        return AuditDataSeverity.PASS, pd.DataFrame()
    else:
        raise ValueError(f"Dataframe type {type(df)} is not supported yet.")

def _audit_negative_values(
    df: AnyDFType,
    target_col: str = "y",
) -> tuple[AuditDataSeverity, AnyDFType]:
    if isinstance(df, pd.DataFrame):
        negative_values = df.loc[df[target_col] < 0]
        if len(negative_values) > 0:
            return AuditDataSeverity.CASE_SPECIFIC, negative_values
        return AuditDataSeverity.PASS, pd.DataFrame()
    else:
        raise ValueError(f"Dataframe type {type(df)} is not supported yet.")

class ApiError(Exception):
    status_code: Optional[int]
    body: Any

    def __init__(
        self, *, status_code: Optional[int] = None, body: Optional[Any] = None
    ):
        self.status_code = status_code
        self.body = body

    def __str__(self) -> str:
        return f"status_code: {self.status_code}, body: {self.body}"

class NixtlaClient:

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[int] = 60,
        max_retries: int = 6,
        retry_interval: int = 10,
        max_wait_time: int = 6 * 60,
    ):
        """
        Client to interact with the Nixtla API.

        Parameters
        ----------
        api_key : str, optional (default=None)
            The authorization api_key interacts with the Nixtla API.
            If not provided, will use the NIXTLA_API_KEY environment variable.
        base_url : str, optional (default=None)
            Custom base_url.
            If not provided, will use the NIXTLA_BASE_URL environment variable.
        timeout : int, optional (default=60)
            Request timeout in seconds. Set this to `None` to disable it.
        max_retries : int (default=6)
            The maximum number of attempts to make when calling the API before giving up.
            It defines how many times the client will retry the API call if it fails.
            Default value is 6, indicating the client will attempt the API call up to 6 times in total
        retry_interval : int (default=10)
            The interval in seconds between consecutive retry attempts.
            This is the waiting period before the client tries to call the API again after a failed attempt.
            Default value is 10 seconds, meaning the client waits for 10 seconds between retries.
        max_wait_time : int (default=360)
            The maximum total time in seconds that the client will spend on all retry attempts before giving up.
            This sets an upper limit on the cumulative waiting time for all retry attempts.
            If this time is exceeded, the client will stop retrying and raise an exception.
            Default value is 360 seconds, meaning the client will cease retrying if the total time
            spent on retries exceeds 360 seconds.
            The client throws a ReadTimeout error after 60 seconds of inactivity. If you want to
            catch these errors, use max_wait_time >> 60.
        """
        if api_key is None:
            api_key = os.environ["NIXTLA_API_KEY"]
        if base_url is None:
            base_url = os.getenv("NIXTLA_BASE_URL", "https://api.nixtla.io")
        self._client_kwargs = {
            "base_url": base_url,
            "headers": {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            "timeout": timeout,
        }
        self._retry_strategy = _retry_strategy(
            max_retries=max_retries,
            retry_interval=retry_interval,
            max_wait_time=max_wait_time,
        )
        self._model_params: dict[tuple[str, str], tuple[int, int]] = {}
        self._is_azure = "ai.azure" in base_url
        self.supported_models: list[Any] = [re.compile("^timegpt-.+$"), "azureai"]

    def _make_request(
        self,
        client: httpx.Client,
        endpoint: str,
        payload: dict[str, Any],
        multithreaded_compress: bool,
    ) -> dict[str, Any]:
        def ensure_contiguous_if_array(x):
            if not isinstance(x, np.ndarray):
                return x
            if np.issubdtype(x.dtype, np.floating):
                x = np.nan_to_num(
                    np.ascontiguousarray(x, dtype=np.float32),
                    nan=np.nan,
                    posinf=np.finfo(np.float32).max,
                    neginf=np.finfo(np.float32).min,
                    copy=False,
                )
            else:
                x = np.ascontiguousarray(x)
            return x

        def ensure_contiguous_arrays(d: dict[str, Any]) -> None:
            for k, v in d.items():
                if isinstance(v, np.ndarray):
                    d[k] = ensure_contiguous_if_array(v)
                elif isinstance(v, list):
                    d[k] = [ensure_contiguous_if_array(x) for x in v]
                elif isinstance(v, dict):
                    ensure_contiguous_arrays(v)

        ensure_contiguous_arrays(payload)
        content = orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY)
        content_size_mb = len(content) / 2**20
        if content_size_mb > 200:
            raise ValueError(
                f"The payload is too large. Set num_partitions={math.ceil(content_size_mb / 200)}"
            )
        headers = {}
        if content_size_mb > 1:
            threads = -1 if multithreaded_compress else 0
            content = zstd.ZstdCompressor(level=1, threads=threads).compress(content)
            headers["content-encoding"] = "zstd"
        resp = client.post(url=endpoint, content=content, headers=headers)
        try:
            resp_body = orjson.loads(resp.content)
        except orjson.JSONDecodeError:
            raise ApiError(
                status_code=resp.status_code,
                body=f"Could not parse JSON: {resp.content}",
            )
        if resp.status_code != 200:
            raise ApiError(status_code=resp.status_code, body=resp_body)
        if "data" in resp_body:
            resp_body = resp_body["data"]
        return resp_body

    def _make_request_with_retries(
        self,
        client: httpx.Client,
        endpoint: str,
        payload: dict[str, Any],
        multithreaded_compress: bool = True,
    ) -> dict[str, Any]:
        return self._retry_strategy(self._make_request)(
            client=client,
            endpoint=endpoint,
            payload=payload,
            multithreaded_compress=multithreaded_compress,
        )

    def _get_request(
        self,
        client: httpx.Client,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        resp = client.get(endpoint, params=params)
        resp_body = resp.json()
        if resp.status_code != 200:
            raise ApiError(status_code=resp.status_code, body=resp_body)
        return resp_body

    def _make_partitioned_requests(
        self,
        client: httpx.Client,
        endpoint: str,
        payloads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from tqdm.auto import tqdm

        num_partitions = len(payloads)
        results: list[dict[str, Any]] = [{} for _ in range(num_partitions)]
        max_workers = min(10, num_partitions)
        with ThreadPoolExecutor(max_workers) as executor:
            future2pos = {
                executor.submit(
                    self._make_request_with_retries,
                    client=client,
                    endpoint=endpoint,
                    payload=payload,
                    multithreaded_compress=False,
                ): i
                for i, payload in enumerate(payloads)
            }
            for future in tqdm(as_completed(future2pos), total=len(future2pos)):
                pos = future2pos[future]
                results[pos] = future.result()
        resp = {"mean": np.hstack([res["mean"] for res in results])}
        first_res = results[0]
        for k in ("sizes", "anomaly"):
            if k in first_res:
                resp[k] = np.hstack([res[k] for res in results])
        if "idxs" in first_res:
            offsets = [0] + [sum(p["series"]["sizes"]) for p in payloads[:-1]]
            resp["idxs"] = np.hstack(
                [
                    np.array(res["idxs"], dtype=np.int64) + offset
                    for res, offset in zip(results, offsets)
                ]
            )
        if "anomaly_score" in first_res:
            resp["anomaly_score"] = np.hstack([res["anomaly_score"] for res in results])
        if first_res["intervals"] is None:
            resp["intervals"] = None
        else:
            resp["intervals"] = {}
            for k in first_res["intervals"].keys():
                resp["intervals"][k] = np.hstack(
                    [res["intervals"][k] for res in results]
                )
        if "weights_x" not in first_res or first_res["weights_x"] is None:
            resp["weights_x"] = None
        else:
            resp["weights_x"] = [res["weights_x"] for res in results]
        if (
            "feature_contributions" not in first_res
            or first_res["feature_contributions"] is None
        ):
            resp["feature_contributions"] = None
        else:
            resp["feature_contributions"] = np.vstack(
                [np.stack(res["feature_contributions"], axis=1) for res in results]
            ).T
        return resp

    def _maybe_override_model(self, model: _Model) -> _Model:
        if self._is_azure and model != "azureai":
            warnings.warn("Azure endpoint detected, setting `model` to 'azureai'.")
            model = "azureai"
        return model

    def _make_client(self, **kwargs: Any) -> httpx.Client:
        return httpx.Client(**kwargs)

    def _get_model_params(self, model: _Model, freq: str) -> tuple[int, int]:
        key = (model, freq)
        if key not in self._model_params:
            logger.info("Querying model metadata...")
            payload = {"model": model, "freq": freq}
            with self._make_client(**self._client_kwargs) as client:
                if self._is_azure:
                    resp_body = self._make_request_with_retries(
                        client, "model_params", payload
                    )
                else:
                    resp_body = self._retry_strategy(self._get_request)(
                        client, "/model_params", payload
                    )
            params = resp_body["detail"]
            self._model_params[key] = (params["input_size"], params["horizon"])
        return self._model_params[key]

    def _maybe_assign_weights(
        self,
        weights: Optional[Union[list[float], list[list[float]]]],
        df: DataFrame,
        x_cols: list[str],
    ) -> None:
        if weights is None:
            return
        if isinstance(weights[0], list):
            self.weights_x = [
                type(df)({"features": x_cols, "weights": w}) for w in weights
            ]
        else:
            self.weights_x = type(df)({"features": x_cols, "weights": weights})

    def _maybe_assign_feature_contributions(
        self,
        expected_contributions: bool,
        resp: dict[str, Any],
        x_cols: list[str],
        out_df: DataFrame,
        insample_feat_contributions: Optional[list[list[float]]],
    ) -> None:
        if not expected_contributions:
            return
        if "feature_contributions" not in resp:
            if self._is_azure:
                warnings.warn("feature_contributions aren't implemented in Azure yet.")
                return
            else:
                raise RuntimeError(
                    "feature_contributions expected in response but not found"
                )
        feature_contributions = resp["feature_contributions"]
        if feature_contributions is None:
            return
        shap_cols = x_cols + ["base_value"]
        shap_df = type(out_df)(dict(zip(shap_cols, feature_contributions)))
        if insample_feat_contributions is not None:
            insample_shap_df = type(out_df)(
                dict(zip(shap_cols, insample_feat_contributions))
            )
            shap_df = ufp.vertical_concat([insample_shap_df, shap_df])
        self.feature_contributions = ufp.horizontal_concat([out_df, shap_df])

    def _run_validations(
        self,
        df: DFType,
        X_df: Optional[DFType],
        id_col: str,
        time_col: str,
        target_col: str,
        model: _Model,
        validate_api_key: bool,
        freq: Optional[_FreqType],
    ) -> tuple[DFType, Optional[DFType], bool, _FreqType]:
        if validate_api_key and not self.validate_api_key(log=False):
            raise Exception("API Key not valid, please email support@nixtla.io")
        if not _model_in_list(model, tuple(self.supported_models)):
            raise ValueError(f"unsupported model: {model}.")
        drop_id = id_col not in df.columns
        if drop_id:
            df = ufp.copy_if_pandas(df, deep=False)
            df = ufp.assign_columns(df, id_col, 0)
            if X_df is not None:
                X_df = ufp.copy_if_pandas(X_df, deep=False)
                X_df = ufp.assign_columns(X_df, id_col, 0)
        if (
            isinstance(df, pd.DataFrame)
            and time_col not in df
            and pd.api.types.is_datetime64_any_dtype(df.index)
        ):
            df.index.name = time_col
            df = df.reset_index()
        df = ensure_time_dtype(df, time_col=time_col)
        validate_format(df=df, id_col=id_col, time_col=time_col, target_col=target_col)
        if ufp.is_nan_or_none(df[target_col]).any():
            raise ValueError(
                f"Target column ({target_col}) cannot contain missing values."
            )
        freq = _maybe_infer_freq(df, freq=freq, id_col=id_col, time_col=time_col)
        if isinstance(freq, (str, int)):
            expected_ids_times = id_time_grid(
                df,
                freq=freq,
                start="per_serie",
                end="per_serie",
                id_col=id_col,
                time_col=time_col,
            )
            freq_ok = len(df) == len(expected_ids_times)
        elif isinstance(freq, pd.offsets.BaseOffset):
            times_by_id = df.groupby(id_col, observed=True)[time_col].agg(
                ["min", "max", "size"]
            )
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
                expected_ends = times_by_id["min"] + freq * (times_by_id["size"] - 1)
            freq_ok = (expected_ends == times_by_id["max"]).all()
        else:
            raise ValueError(
                "`freq` should be a string, integer or pandas offset, "
                f"got {type(freq).__name__}."
            )
        if not freq_ok:
            raise ValueError(
                "Series contain missing or duplicate timestamps, or the timestamps "
                "do not match the provided frequency.\n"
                "Please make sure that all series have a single observation from the first "
                "to the last timestamp and that the provided frequency matches the timestamps'.\n"
                "You can refer to https://docs.nixtla.io/docs/tutorials-missing_values "
                "for an end to end example."
            )
        return df, X_df, drop_id, freq

    def validate_api_key(self, log: bool = True) -> bool:
        """Check API key status.

        Parameters
        ----------
        log : bool (default=True)
            Show the endpoint's response.

        Returns
        -------
        bool
            Whether API key is valid."""
        if self._is_azure:
            raise NotImplementedError(
                "validate_api_key is not implemented for Azure deployments, "
                "you can try using the forecasting methods directly."
            )
        with self._make_client(**self._client_kwargs) as client:
            resp = client.get("/validate_api_key")
            body = resp.json()
        if log:
            logger.info(body["detail"])
        return resp.status_code == 200

    def usage(self) -> dict[str, dict[str, int]]:
        """Query consumed requests and limits

        Returns
        -------
        dict
            Consumed requests and limits by minute and month."""
        if self._is_azure:
            raise NotImplementedError("usage is not implemented for Azure deployments")
        with self._make_client(**self._client_kwargs) as client:
            return self._get_request(client, "/usage")

    def finetune(
        self,
        df: DataFrame,
        freq: Optional[_Freq] = None,
        id_col: str = "unique_id",
        time_col: str = "ds",
        target_col: str = "y",
        finetune_steps: _NonNegativeInt = 10,
        finetune_depth: _FinetuneDepth = 1,
        finetune_loss: _Loss = "default",
        output_model_id: Optional[str] = None,
        finetuned_model_id: Optional[str] = None,
        model: _Model = "timegpt-1",
    ) -> str:
        """Fine-tune TimeGPT to your series.

        Parameters
        ----------
        df : pandas or polars DataFrame
            The DataFrame on which the function will operate. Expected to contain at least the following columns:
            - time_col:
                Column name in `df` that contains the time indices of the time series. This is typically a datetime
                column with regular intervals, e.g., hourly, daily, monthly data points.
            - target_col:
                Column name in `df` that contains the target variable of the time series, i.e., the variable we
                wish to predict or analyze.
            Additionally, you can pass multiple time series (stacked in the dataframe) considering an additional column:
            - id_col:
                Column name in `df` that identifies unique time series. Each unique value in this column
                corresponds to a unique time series.
        freq : str, int or pandas offset, optional (default=None).
            Frequency of the timestamps. If `None`, it will be inferred automatically.
            See [pandas' available frequencies](https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#offset-aliases).
        id_col : str (default='unique_id')
            Column that identifies each series.
        time_col : str (default='ds')
            Column that identifies each timestep, its values can be timestamps or integers.
        target_col : str (default='y')
            Column that contains the target.
        finetune_steps : int (default=10)
            Number of steps used to finetune learning TimeGPT in the new data.
        finetune_depth : int (default=1)
            The depth of the finetuning. Uses a scale from 1 to 5, where 1 means little finetuning,
            and 5 means that the entire model is finetuned.
        finetune_loss : str (default='default')
            Loss function to use for finetuning. Options are: `default`, `mae`, `mse`, `rmse`, `mape`, and `smape`.
        output_model_id : str, optional(default=None)
            ID to assign to the fine-tuned model. If `None`, an UUID is used.
        finetuned_model_id : str, optional(default=None)
            ID of previously fine-tuned model to use as base.
        model : str (default='timegpt-1')
            Model to use as a string. Options are: `timegpt-1`, and `timegpt-1-long-horizon`.
            We recommend using `timegpt-1-long-horizon` for forecasting
            if you want to predict more than one seasonal
            period given the frequency of your data.

        Returns
        -------
        str
            ID of the fine-tuned model
        """
        if not isinstance(df, (pd.DataFrame, pl_DataFrame)):
            raise ValueError("Can only fine-tune on pandas or polars dataframes.")
        model = self._maybe_override_model(model)
        logger.info("Validating inputs...")
        df, X_df, drop_id, freq = self._run_validations(
            df=df,
            X_df=None,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            validate_api_key=False,
            model=model,
            freq=freq,
        )

        logger.info("Preprocessing dataframes...")
        processed, *_ = _preprocess(
            df=df,
            X_df=None,
            h=0,
            freq=freq,
            date_features=False,
            date_features_to_one_hot=False,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
        )
        standard_freq = _standardize_freq(freq, processed)
        model_input_size, model_horizon = self._get_model_params(model, standard_freq)
        _validate_input_size(processed, model_input_size, model_horizon)
        logger.info("Calling Fine-tune Endpoint...")
        payload = {
            "series": {
                "y": processed.data[:, 0],
                "sizes": np.diff(processed.indptr),
            },
            "model": model,
            "freq": standard_freq,
            "finetune_steps": finetune_steps,
            "finetune_depth": finetune_depth,
            "finetune_loss": finetune_loss,
            "output_model_id": output_model_id,
            "finetuned_model_id": finetuned_model_id,
        }
        with self._make_client(**self._client_kwargs) as client:
            resp = self._make_request_with_retries(client, "v2/finetune", payload)
        return resp["finetuned_model_id"]

    @overload
    def finetuned_models(self, as_df: Literal[False]) -> list[FinetunedModel]: ...

    @overload
    def finetuned_models(self, as_df: Literal[True]) -> pd.DataFrame: ...

    def finetuned_models(
        self,
        as_df: bool = False,
    ) -> Union[list[FinetunedModel], pd.DataFrame]:
        """List fine-tuned models

        Parameters
        ----------
        as_df : bool
            Return the fine-tuned models as a pandas dataframe

        Returns
        -------
        list of FinetunedModel
            List of available fine-tuned models."""
        with self._make_client(**self._client_kwargs) as client:
            resp_body = self._get_request(client, "/v2/finetuned_models")
        models = [FinetunedModel(**m) for m in resp_body["finetuned_models"]]
        if as_df:
            models = pd.DataFrame([m.model_dump() for m in models])
        return models

    def finetuned_model(self, finetuned_model_id: str) -> FinetunedModel:
        """Get fine-tuned model metadata

        Parameters
        ----------
        finetuned_model_id : str
            ID of the fine-tuned model to get metadata from.

        Returns
        -------
        FinetunedModel
            Fine-tuned model metadata."""
        with self._make_client(**self._client_kwargs) as client:
            resp_body = self._get_request(
                client, f"/v2/finetuned_models/{finetuned_model_id}"
            )
        return FinetunedModel(**resp_body)

    def delete_finetuned_model(self, finetuned_model_id: str) -> bool:
        """Delete a previously fine-tuned model

        Parameters
        ----------
        finetuned_model_id : str
            ID of the fine-tuned model to be deleted.

        Returns
        -------
        bool
            Whether delete was successful."""
        with self._make_client(**self._client_kwargs) as client:
            resp = client.delete(
                f"/v2/finetuned_models/{finetuned_model_id}",
                headers={"accept-encoding": "identity"},
            )
        return resp.status_code == 204

    def _distributed_forecast(
        self,
        df: DistributedDFType,
        h: _PositiveInt,
        freq: Optional[_Freq],
        id_col: str,
        time_col: str,
        target_col: str,
        X_df: Optional[DistributedDFType],
        level: Optional[list[Union[int, float]]],
        quantiles: Optional[list[float]],
        finetune_steps: _NonNegativeInt,
        finetune_depth: _FinetuneDepth,
        finetune_loss: _Loss,
        finetuned_model_id: Optional[str],
        clean_ex_first: bool,
        hist_exog_list: Optional[list[str]],
        validate_api_key: bool,
        add_history: bool,
        date_features: Union[bool, list[Union[str, Callable]]],
        date_features_to_one_hot: Union[bool, list[str]],
        model: _Model,
        num_partitions: Optional[int],
        feature_contributions: bool,
    ) -> DistributedDFType:
        import fugue.api as fa

        schema, partition_config = _distributed_setup(
            df=df,
            method="forecast",
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            level=level,
            quantiles=quantiles,
            num_partitions=num_partitions,
        )
        if X_df is not None:

            def format_df(df: pd.DataFrame) -> pd.DataFrame:
                return df.assign(_in_sample=True)

            def format_X_df(
                X_df: pd.DataFrame,
                target_col: str,
                df_cols: list[str],
            ) -> pd.DataFrame:
                return X_df.assign(**{"_in_sample": False, target_col: 0.0})[df_cols]

            df = fa.transform(df, format_df, schema="*,_in_sample:bool")
            X_df = fa.transform(
                X_df,
                format_X_df,
                schema=fa.get_schema(df),
                params={"target_col": target_col, "df_cols": fa.get_column_names(df)},
            )
            df = fa.union(df, X_df)
        result_df = fa.transform(
            df,
            using=_forecast_wrapper,
            schema=schema,
            params=dict(
                client=self,
                h=h,
                freq=freq,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
                level=level,
                quantiles=quantiles,
                finetune_steps=finetune_steps,
                finetune_depth=finetune_depth,
                finetune_loss=finetune_loss,
                finetuned_model_id=finetuned_model_id,
                clean_ex_first=clean_ex_first,
                hist_exog_list=hist_exog_list,
                validate_api_key=validate_api_key,
                add_history=add_history,
                date_features=date_features,
                date_features_to_one_hot=date_features_to_one_hot,
                model=model,
                num_partitions=None,
                feature_contributions=feature_contributions,
            ),
            partition=partition_config,
            as_fugue=True,
        )
        return fa.get_native_as_df(result_df)

    def forecast(
        self,
        df: AnyDFType,
        h: _PositiveInt,
        freq: Optional[_Freq] = None,
        id_col: str = "unique_id",
        time_col: str = "ds",
        target_col: str = "y",
        X_df: Optional[AnyDFType] = None,
        level: Optional[list[Union[int, float]]] = None,
        quantiles: Optional[list[float]] = None,
        finetune_steps: _NonNegativeInt = 0,
        finetune_depth: _FinetuneDepth = 1,
        finetune_loss: _Loss = "default",
        finetuned_model_id: Optional[str] = None,
        clean_ex_first: bool = True,
        hist_exog_list: Optional[list[str]] = None,
        validate_api_key: bool = False,
        add_history: bool = False,
        date_features: Union[bool, list[Union[str, Callable]]] = False,
        date_features_to_one_hot: Union[bool, list[str]] = False,
        model: _Model = "timegpt-1",
        num_partitions: Optional[_PositiveInt] = None,
        feature_contributions: bool = False,
    ) -> AnyDFType:
        """Forecast your time series using TimeGPT.

        Parameters
        ----------
        df : pandas or polars DataFrame
            The DataFrame on which the function will operate. Expected to contain at least the following columns:
            - time_col:
                Column name in `df` that contains the time indices of the time series. This is typically a datetime
                column with regular intervals, e.g., hourly, daily, monthly data points.
            - target_col:
                Column name in `df` that contains the target variable of the time series, i.e., the variable we
                wish to predict or analyze.
            Additionally, you can pass multiple time series (stacked in the dataframe) considering an additional column:
            - id_col:
                Column name in `df` that identifies unique time series. Each unique value in this column
                corresponds to a unique time series.
        h : int
            Forecast horizon.
        freq : str, int or pandas offset, optional (default=None).
            Frequency of the timestamps. If `None`, it will be inferred automatically.
            See [pandas' available frequencies](https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#offset-aliases).
        id_col : str (default='unique_id')
            Column that identifies each series.
        time_col : str (default='ds')
            Column that identifies each timestep, its values can be timestamps or integers.
        target_col : str (default='y')
            Column that contains the target.
        X_df : pandas or polars DataFrame, optional (default=None)
            DataFrame with [`unique_id`, `ds`] columns and `df`'s future exogenous.
        level : list[float], optional (default=None)
            Confidence levels between 0 and 100 for prediction intervals.
        quantiles : list[float], optional (default=None)
            Quantiles to forecast, list between (0, 1).
            `level` and `quantiles` should not be used simultaneously.
            The output dataframe will have the quantile columns
            formatted as TimeGPT-q-(100 * q) for each q.
            100 * q represents percentiles but we choose this notation
            to avoid having dots in column names.
        finetune_steps : int (default=0)
            Number of steps used to finetune learning TimeGPT in the
            new data.
        finetune_depth : int (default=1)
            The depth of the finetuning. Uses a scale from 1 to 5, where 1 means little finetuning,
            and 5 means that the entire model is finetuned.
        finetune_loss : str (default='default')
            Loss function to use for finetuning. Options are: `default`, `mae`, `mse`, `rmse`, `mape`, and `smape`.
        finetuned_model_id : str, optional(default=None)
            ID of previously fine-tuned model to use.
        clean_ex_first : bool (default=True)
            Clean exogenous signal before making forecasts using TimeGPT.
        hist_exog_list : list of str, optional (default=None)
            Column names of the historical exogenous features.
        validate_api_key : bool (default=False)
            If True, validates api_key before sending requests.
        add_history : bool (default=False)
            Return fitted values of the model.
        date_features : bool or list of str or callable, optional (default=False)
            Features computed from the dates.
            Can be pandas date attributes or functions that will take the dates as input.
            If True automatically adds most used date features for the
            frequency of `df`.
        date_features_to_one_hot : bool or list of str (default=False)
            Apply one-hot encoding to these date features.
            If `date_features=True`, then all date features are
            one-hot encoded by default.
        model : str (default='timegpt-1')
            Model to use as a string. Options are: `timegpt-1`, and `timegpt-1-long-horizon`.
            We recommend using `timegpt-1-long-horizon` for forecasting
            if you want to predict more than one seasonal
            period given the frequency of your data.
        num_partitions : int (default=None)
            Number of partitions to use.
            If None, the number of partitions will be equal
            to the available parallel resources in distributed environments.
        feature_contributions: bool (default=False)
            Compute SHAP values
            Gives access to computed SHAP values to explain the impact
            of features on the final predictions.

        Returns
        -------
        pandas, polars, dask or spark DataFrame or ray Dataset.
            DataFrame with TimeGPT forecasts for point predictions and probabilistic
            predictions (if level is not None).
        """
        if not isinstance(df, (pd.DataFrame, pl_DataFrame)):
            return self._distributed_forecast(
                df=df,
                h=h,
                freq=freq,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
                X_df=X_df,
                level=level,
                quantiles=quantiles,
                finetune_steps=finetune_steps,
                finetune_depth=finetune_depth,
                finetune_loss=finetune_loss,
                finetuned_model_id=finetuned_model_id,
                clean_ex_first=clean_ex_first,
                hist_exog_list=hist_exog_list,
                validate_api_key=validate_api_key,
                add_history=add_history,
                date_features=date_features,
                date_features_to_one_hot=date_features_to_one_hot,
                model=model,
                num_partitions=num_partitions,
                feature_contributions=feature_contributions,
            )
        self.__dict__.pop("weights_x", None)
        self.__dict__.pop("feature_contributions", None)
        model = self._maybe_override_model(model)
        logger.info("Validating inputs...")
        df, X_df, drop_id, freq = self._run_validations(
            df=df,
            X_df=X_df,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            validate_api_key=validate_api_key,
            model=model,
            freq=freq,
        )
        df, X_df = _validate_exog(
            df=df,
            X_df=X_df,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            hist_exog=hist_exog_list,
        )
        level, quantiles = _prepare_level_and_quantiles(level, quantiles)

        logger.info("Preprocessing dataframes...")
        processed, X_future, x_cols, futr_cols = _preprocess(
            df=df,
            X_df=X_df,
            h=h,
            freq=freq,
            date_features=date_features,
            date_features_to_one_hot=date_features_to_one_hot,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
        )
        standard_freq = _standardize_freq(freq, processed)
        model_input_size, model_horizon = self._get_model_params(model, standard_freq)
        if finetune_steps > 0 or add_history:
            _validate_input_size(processed, model_input_size, model_horizon)
        if h > model_horizon:
            logger.warning(
                'The specified horizon "h" exceeds the model horizon, '
                "this may lead to less accurate forecasts. "
                "Please consider using a smaller horizon."
            )
        restrict_input = finetune_steps == 0 and not x_cols and not add_history
        if restrict_input:
            logger.info("Restricting input...")
            new_input_size = _restrict_input_samples(
                level=level,
                input_size=model_input_size,
                model_horizon=model_horizon,
                h=h,
            )
            processed = _tail(processed, new_input_size)
        if processed.data.shape[1] > 1:
            X = processed.data[:, 1:].T
            if futr_cols is not None:
                logger.info(f"Using future exogenous features: {futr_cols}")
            if hist_exog_list:
                logger.info(f"Using historical exogenous features: {hist_exog_list}")
        else:
            X = None

        logger.info("Calling Forecast Endpoint...")
        payload = {
            "series": {
                "y": processed.data[:, 0],
                "sizes": np.diff(processed.indptr),
                "X": X,
                "X_future": X_future,
            },
            "model": model,
            "h": h,
            "freq": standard_freq,
            "clean_ex_first": clean_ex_first,
            "level": level,
            "finetune_steps": finetune_steps,
            "finetune_depth": finetune_depth,
            "finetune_loss": finetune_loss,
            "finetuned_model_id": finetuned_model_id,
            "feature_contributions": feature_contributions and X is not None,
        }
        with self._make_client(**self._client_kwargs) as client:
            insample_feat_contributions = None
            if num_partitions is None:
                resp = self._make_request_with_retries(client, "v2/forecast", payload)
                if add_history:
                    in_sample_payload = _forecast_payload_to_in_sample(payload)
                    logger.info("Calling Historical Forecast Endpoint...")
                    in_sample_resp = self._make_request_with_retries(
                        client, "v2/historic_forecast", in_sample_payload
                    )
                    insample_feat_contributions = in_sample_resp.get(
                        "feature_contributions", None
                    )
            else:
                payloads = _partition_series(payload, num_partitions, h)
                resp = self._make_partitioned_requests(client, "v2/forecast", payloads)
                if add_history:
                    in_sample_payloads = [
                        _forecast_payload_to_in_sample(p) for p in payloads
                    ]
                    logger.info("Calling Historical Forecast Endpoint...")
                    in_sample_resp = self._make_partitioned_requests(
                        client, "v2/historic_forecast", in_sample_payloads
                    )
                    insample_feat_contributions = in_sample_resp.get(
                        "feature_contributions", None
                    )

        # assemble result
        out = ufp.make_future_dataframe(
            uids=processed.uids,
            last_times=type(processed.uids)(processed.last_times),
            freq=freq,
            h=h,
            id_col=id_col,
            time_col=time_col,
        )
        out = ufp.assign_columns(out, "TimeGPT", resp["mean"])
        out = _maybe_add_intervals(out, resp["intervals"])
        if add_history:
            in_sample_df = _parse_in_sample_output(
                in_sample_output=in_sample_resp,
                df=df,
                processed=processed,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
            )
            in_sample_df = ufp.drop_columns(in_sample_df, target_col)
            out = ufp.vertical_concat([in_sample_df, out])
        out = _maybe_convert_level_to_quantiles(out, quantiles)
        self._maybe_assign_feature_contributions(
            expected_contributions=feature_contributions,
            resp=resp,
            x_cols=x_cols,
            out_df=out[[id_col, time_col, "TimeGPT"]],
            insample_feat_contributions=insample_feat_contributions,
        )
        if add_history:
            sort_idxs = ufp.maybe_compute_sort_indices(
                out, id_col=id_col, time_col=time_col
            )
            if sort_idxs is not None:
                out = ufp.take_rows(out, sort_idxs)
                out = ufp.drop_index_if_pandas(out)
                if hasattr(self, "feature_contributions"):
                    self.feature_contributions = ufp.take_rows(
                        self.feature_contributions, sort_idxs
                    )
                    self.feature_contributions = ufp.drop_index_if_pandas(
                        self.feature_contributions
                    )
        out = _maybe_drop_id(df=out, id_col=id_col, drop=drop_id)
        self._maybe_assign_weights(weights=resp["weights_x"], df=df, x_cols=x_cols)
        return out

    def _distributed_detect_anomalies(
        self,
        df: DistributedDFType,
        freq: Optional[_Freq],
        id_col: str,
        time_col: str,
        target_col: str,
        level: Union[int, float],
        finetuned_model_id: Optional[str],
        clean_ex_first: bool,
        validate_api_key: bool,
        date_features: Union[bool, list[str]],
        date_features_to_one_hot: Union[bool, list[str]],
        model: _Model,
        num_partitions: Optional[int],
    ) -> DistributedDFType:
        import fugue.api as fa

        schema, partition_config = _distributed_setup(
            df=df,
            method="detect_anomalies",
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            level=level,
            quantiles=None,
            num_partitions=num_partitions,
        )
        result_df = fa.transform(
            df,
            using=_detect_anomalies_wrapper,
            schema=schema,
            params=dict(
                client=self,
                freq=freq,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
                level=level,
                finetuned_model_id=finetuned_model_id,
                clean_ex_first=clean_ex_first,
                validate_api_key=validate_api_key,
                date_features=date_features,
                date_features_to_one_hot=date_features_to_one_hot,
                model=model,
                num_partitions=None,
            ),
            partition=partition_config,
            as_fugue=True,
        )
        return fa.get_native_as_df(result_df)

    def detect_anomalies(
        self,
        df: AnyDFType,
        freq: Optional[_Freq] = None,
        id_col: str = "unique_id",
        time_col: str = "ds",
        target_col: str = "y",
        level: Union[int, float] = 99,
        finetuned_model_id: Optional[str] = None,
        clean_ex_first: bool = True,
        validate_api_key: bool = False,
        date_features: Union[bool, list[str]] = False,
        date_features_to_one_hot: Union[bool, list[str]] = False,
        model: _Model = "timegpt-1",
        num_partitions: Optional[_PositiveInt] = None,
    ) -> AnyDFType:
        """Detect anomalies in your time series using TimeGPT.

        Parameters
        ----------
        df : pandas or polars DataFrame
            The DataFrame on which the function will operate. Expected to contain at least the following columns:
            - time_col:
                Column name in `df` that contains the time indices of the time series. This is typically a datetime
                column with regular intervals, e.g., hourly, daily, monthly data points.
            - target_col:
                Column name in `df` that contains the target variable of the time series, i.e., the variable we
                wish to predict or analyze.
            Additionally, you can pass multiple time series (stacked in the dataframe) considering an additional column:
            - id_col:
                Column name in `df` that identifies unique time series. Each unique value in this column
                corresponds to a unique time series.
        freq : str, int or pandas offset, optional (default=None).
            Frequency of the timestamps. If `None`, it will be inferred automatically.
            See [pandas' available frequencies](https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#offset-aliases).
        id_col : str (default='unique_id')
            Column that identifies each series.
        time_col : str (default='ds')
            Column that identifies each timestep, its values can be timestamps or integers.
        target_col : str (default='y')
            Column that contains the target.
        level : float (default=99)
            Confidence level between 0 and 100 for detecting the anomalies.
        finetuned_model_id : str, optional(default=None)
            ID of previously fine-tuned model to use.
        clean_ex_first : bool (default=True)
            Clean exogenous signal before making forecasts
            using TimeGPT.
        validate_api_key : bool (default=False)
            If True, validates api_key before sending requests.
        date_features : bool or list of str or callable, optional (default=False)
            Features computed from the dates.
            Can be pandas date attributes or functions that will take the dates as input.
            If True automatically adds most used date features for the
            frequency of `df`.
        date_features_to_one_hot : bool or list of str (default=False)
            Apply one-hot encoding to these date features.
            If `date_features=True`, then all date features are
            one-hot encoded by default.
        model : str (default='timegpt-1')
            Model to use as a string. Options are: `timegpt-1`, and `timegpt-1-long-horizon`.
            We recommend using `timegpt-1-long-horizon` for forecasting
            if you want to predict more than one seasonal
            period given the frequency of your data.
        num_partitions : int (default=None)
            Number of partitions to use.
            If None, the number of partitions will be equal
            to the available parallel resources in distributed environments.

        Returns
        -------
        pandas, polars, dask or spark DataFrame or ray Dataset.
            DataFrame with anomalies flagged by TimeGPT.
        """
        if not isinstance(df, (pd.DataFrame, pl_DataFrame)):
            return self._distributed_detect_anomalies(
                df=df,
                freq=freq,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
                level=level,
                finetuned_model_id=finetuned_model_id,
                clean_ex_first=clean_ex_first,
                validate_api_key=validate_api_key,
                date_features=date_features,
                date_features_to_one_hot=date_features_to_one_hot,
                model=model,
                num_partitions=num_partitions,
            )
        self.__dict__.pop("weights_x", None)
        model = self._maybe_override_model(model)
        logger.info("Validating inputs...")
        df, _, drop_id, freq = self._run_validations(
            df=df,
            X_df=None,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            validate_api_key=validate_api_key,
            model=model,
            freq=freq,
        )

        logger.info("Preprocessing dataframes...")
        processed, _, x_cols, _ = _preprocess(
            df=df,
            X_df=None,
            h=0,
            freq=freq,
            date_features=date_features,
            date_features_to_one_hot=date_features_to_one_hot,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
        )
        standard_freq = _standardize_freq(freq, processed)
        model_input_size, model_horizon = self._get_model_params(model, standard_freq)
        if processed.data.shape[1] > 1:
            X = processed.data[:, 1:].T
            logger.info(f"Using the following exogenous features: {x_cols}")
        else:
            X = None

        logger.info("Calling Anomaly Detector Endpoint...")
        payload = {
            "series": {
                "y": processed.data[:, 0],
                "sizes": np.diff(processed.indptr),
                "X": X,
            },
            "model": model,
            "freq": standard_freq,
            "finetuned_model_id": finetuned_model_id,
            "clean_ex_first": clean_ex_first,
            "level": level,
        }
        with self._make_client(**self._client_kwargs) as client:
            if num_partitions is None:
                resp = self._make_request_with_retries(
                    client, "v2/anomaly_detection", payload
                )
            else:
                payloads = _partition_series(payload, num_partitions, h=0)
                resp = self._make_partitioned_requests(
                    client, "v2/anomaly_detection", payloads
                )

        # assemble result
        out = _parse_in_sample_output(
            in_sample_output=resp,
            df=df,
            processed=processed,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
        )
        out = ufp.assign_columns(out, "anomaly", resp["anomaly"])
        out = _maybe_drop_id(df=out, id_col=id_col, drop=drop_id)
        self._maybe_assign_weights(weights=resp["weights_x"], df=df, x_cols=x_cols)
        return out

    def _distributed_detect_anomalies_online(
        self,
        df: DistributedDFType,
        h: _PositiveInt,
        detection_size: _PositiveInt,
        threshold_method: _ThresholdMethod,
        freq: Optional[_Freq],
        id_col: str,
        time_col: str,
        target_col: str,
        level: Union[int, float],
        clean_ex_first: bool,
        step_size: Optional[_PositiveInt],
        finetune_steps: _NonNegativeInt,
        finetune_depth: _FinetuneDepth,
        finetune_loss: _Loss,
        hist_exog_list: Optional[list[str]],
        date_features: Union[bool, list[str]],
        date_features_to_one_hot: Union[bool, list[str]],
        model: _Model,
        refit: bool,
        num_partitions: Optional[int],
    ) -> DistributedDFType:
        import fugue.api as fa

        schema, partition_config = _distributed_setup(
            df=df,
            method="detect_anomalies_online",
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            level=level,
            quantiles=None,
            num_partitions=num_partitions,
        )
        result_df = fa.transform(
            df,
            using=_detect_anomalies_online_wrapper,
            schema=schema,
            params=dict(
                client=self,
                h=h,
                detection_size=detection_size,
                threshold_method=threshold_method,
                freq=freq,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
                level=level,
                clean_ex_first=clean_ex_first,
                step_size=step_size,
                finetune_steps=finetune_steps,
                finetune_loss=finetune_loss,
                finetune_depth=finetune_depth,
                hist_exog_list=hist_exog_list,
                date_features=date_features,
                date_features_to_one_hot=date_features_to_one_hot,
                model=model,
                refit=refit,
                num_partitions=None,
            ),
            partition=partition_config,
            as_fugue=True,
        )
        return fa.get_native_as_df(result_df)

    def detect_anomalies_online(
        self,
        df: AnyDFType,
        h: _PositiveInt,
        detection_size: _PositiveInt,
        threshold_method: _ThresholdMethod = "univariate",
        freq: Optional[_Freq] = None,
        id_col: str = "unique_id",
        time_col: str = "ds",
        target_col: str = "y",
        level: Union[int, float] = 99,
        clean_ex_first: bool = True,
        step_size: Optional[_PositiveInt] = None,
        finetune_steps: _NonNegativeInt = 0,
        finetune_depth: _FinetuneDepth = 1,
        finetune_loss: _Loss = "default",
        hist_exog_list: Optional[list[str]] = None,
        date_features: Union[bool, list[str]] = False,
        date_features_to_one_hot: Union[bool, list[str]] = False,
        model: _Model = "timegpt-1",
        refit: bool = False,
        num_partitions: Optional[_PositiveInt] = None,
    ) -> AnyDFType:
        """
        Online anomaly detection in your time series using TimeGPT.

        Parameters
        ----------
        df : pandas or polars DataFrame
            The DataFrame on which the function will operate. Expected to contain at least the following columns:
            - time_col:
                Column name in `df` that contains the time indices of the time series. This is typically a datetime
                column with regular intervals, e.g., hourly, daily, monthly data points.
            - target_col:
                Column name in `df` that contains the target variable of the time series, i.e., the variable we
                wish to predict or analyze.
            - id_col:
                Column name in `df` that identifies unique time series. Each unique value in this column
                corresponds to a unique time series.
        h : int
            Forecast horizon.
        detection_size : int
            The length of the sequence where anomalies will be detected starting from the end of the dataset.
        threshold_method : str, optional (default='univariate')
            The method used to calculate the intervals for anomaly detection.
            Use `univariate` to flag anomalies independently for each series in the dataset.
            Use `multivariate` to have a global threshold across all series in the dataset. For this method, all series
            must have the same length.
        freq : str, optional
            Frequency of the data. By default, the freq will be inferred automatically.
            See [pandas' available frequencies](https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#offset-aliases).
        id_col : str, optional (default='unique_id')
            Column that identifies each series.
        time_col : str, optional (default='ds')
            Column that identifies each timestep, its values can be timestamps or integers.
        target_col : str, optional (default='y')
            Column that contains the target.
        level : float, optional (default=99)
            Confidence level between 0 and 100 for detecting the anomalies.
        clean_ex_first : bool, optional (default=True)
            Clean exogenous signal before making forecasts using TimeGPT.
        step_size : int, optional (default=None)
            Step size between each cross validation window. If None it will be equal to `h`.
        finetune_steps : int (default=0)
            Number of steps used to finetune TimeGPT in the
            new data.
        finetune_depth : int (default=1)
            The depth of the finetuning. Uses a scale from 1 to 5, where 1 means little finetuning,
            and 5 means that the entire model is finetuned.
        finetune_loss : str (default='default')
            Loss function to use for finetuning. Options are: `default`, `mae`, `mse`, `rmse`, `mape`, and `smape`.
        hist_exog_list : list of str, optional (default=None)
            Column names of the historical exogenous features.
        date_features : bool or list of str, optional (default=False)
            Features computed from the dates.
            Can be pandas date attributes or functions that will take the dates as input.
            If True, automatically adds most used date features for the frequency of `df`.
        date_features_to_one_hot : bool or list of str, optional (default=False)
            Apply one-hot encoding to these date features.
            If `date_features=True`, then all date features are one-hot encoded by default.
        model : str, optional (default='timegpt-1')
            Model to use as a string. Options are: `timegpt-1`, and `timegpt-1-long-horizon`.
            We recommend using `timegpt-1-long-horizon` for forecasting if you want to predict more than one seasonal
            period given the frequency of your data.
        refit : bool, optional (default=False)
            Fine-tune the model in each window. If False, only fine-tunes on the first window.
            Only used if finetune_steps > 0.e
        num_partitions : int, optional (default=None)
            Number of partitions to use.
            If None, the number of partitions will be equal to the available parallel resources in distributed environments.

        Returns
        -------
        pandas, polars, dask or spark DataFrame or ray Dataset
            DataFrame with anomalies flagged by TimeGPT.
        """
        if not isinstance(df, (pd.DataFrame, pl_DataFrame)):
            return self._distributed_detect_anomalies_online(
                df=df,
                h=h,
                detection_size=detection_size,
                threshold_method=threshold_method,
                freq=freq,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
                level=level,
                clean_ex_first=clean_ex_first,
                step_size=step_size,
                finetune_steps=finetune_steps,
                finetune_depth=finetune_depth,
                finetune_loss=finetune_loss,
                hist_exog_list=hist_exog_list,
                date_features=date_features,
                date_features_to_one_hot=date_features_to_one_hot,
                model=model,
                refit=refit,
                num_partitions=num_partitions,
            )
        if (
            threshold_method == "multivariate"
            and num_partitions is not None
            and num_partitions > 1
        ):
            raise ValueError(
                "Cannot use more than 1 partition for multivariate anomaly detection. "
                "Either set threshold_method to univariate "
                "or set num_partitions to None."
            )
        self.__dict__.pop("weights_x", None)
        model = self._maybe_override_model(model)
        logger.info("Validating inputs...")
        df, _, drop_id, freq = self._run_validations(
            df=df,
            X_df=None,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            validate_api_key=False,
            model=model,
            freq=freq,
        )
        logger.info("Preprocessing dataframes...")
        processed, _, x_cols, _ = _preprocess(
            df=df,
            X_df=None,
            h=0,
            freq=freq,
            date_features=date_features,
            date_features_to_one_hot=date_features_to_one_hot,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
        )
        standard_freq = _standardize_freq(freq, processed)
        targets = _extract_target_array(df, target_col)
        times = df[time_col].to_numpy()
        if processed.sort_idxs is not None:
            targets = targets[processed.sort_idxs]
            times = times[processed.sort_idxs]
        X, hist_exog = _process_exog_features(processed.data, x_cols, hist_exog_list)
        sizes = np.diff(processed.indptr)
        if np.all(sizes <= 6 * detection_size):
            logger.warn(
                "Detection size is large. Using the entire series to compute the anomaly threshold..."
            )
        logger.info("Calling Online Anomaly Detector Endpoint...")
        payload = {
            "series": {
                "y": processed.data[:, 0],
                "sizes": sizes,
                "X": X,
            },
            "h": h,
            "detection_size": detection_size,
            "threshold_method": threshold_method,
            "model": model,
            "freq": standard_freq,
            "clean_ex_first": clean_ex_first,
            "level": level,
            "step_size": step_size,
            "finetune_steps": finetune_steps,
            "finetune_loss": finetune_loss,
            "finetune_depth": finetune_depth,
            "refit": refit,
            "hist_exog": hist_exog,
        }
        with self._make_client(**self._client_kwargs) as client:
            if num_partitions is None:
                resp = self._make_request_with_retries(
                    client, "v2/online_anomaly_detection", payload
                )
            else:
                payloads = _partition_series(payload, num_partitions, h=0)
                resp = self._make_partitioned_requests(
                    client, "v2/online_anomaly_detection", payloads
                )

        # assemble result
        idxs = np.array(resp["idxs"], dtype=np.int64)
        sizes = np.array(resp["sizes"], dtype=np.int64)
        out = type(df)(
            {
                id_col: ufp.repeat(processed.uids, sizes),
                time_col: times[idxs],
                target_col: targets[idxs],
            }
        )
        out = ufp.assign_columns(out, "TimeGPT", resp["mean"])
        out = ufp.assign_columns(out, "anomaly", resp["anomaly"])
        out = ufp.assign_columns(out, "anomaly_score", resp["anomaly_score"])
        if threshold_method == "multivariate":
            out = ufp.assign_columns(
                out, "accumulated_anomaly_score", resp["accumulated_anomaly_score"]
            )
        return _maybe_add_intervals(out, resp["intervals"])

    def _distributed_cross_validation(
        self,
        df: DistributedDFType,
        h: _PositiveInt,
        freq: Optional[_Freq],
        id_col: str,
        time_col: str,
        target_col: str,
        level: Optional[list[Union[int, float]]],
        quantiles: Optional[list[float]],
        validate_api_key: bool,
        n_windows: _PositiveInt,
        step_size: Optional[_PositiveInt],
        finetune_steps: _NonNegativeInt,
        finetune_depth: _FinetuneDepth,
        finetune_loss: _Loss,
        finetuned_model_id: Optional[str],
        refit: bool,
        clean_ex_first: bool,
        hist_exog_list: Optional[list[str]],
        date_features: Union[bool, Sequence[Union[str, Callable]]],
        date_features_to_one_hot: Union[bool, list[str]],
        model: _Model,
        num_partitions: Optional[int],
    ) -> DistributedDFType:
        import fugue.api as fa

        schema, partition_config = _distributed_setup(
            df=df,
            method="cross_validation",
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            level=level,
            quantiles=quantiles,
            num_partitions=num_partitions,
        )
        result_df = fa.transform(
            df,
            using=_cross_validation_wrapper,
            schema=schema,
            params=dict(
                client=self,
                h=h,
                freq=freq,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
                level=level,
                quantiles=quantiles,
                validate_api_key=validate_api_key,
                n_windows=n_windows,
                step_size=step_size,
                finetune_steps=finetune_steps,
                finetune_depth=finetune_depth,
                finetune_loss=finetune_loss,
                finetuned_model_id=finetuned_model_id,
                refit=refit,
                clean_ex_first=clean_ex_first,
                hist_exog_list=hist_exog_list,
                date_features=date_features,
                date_features_to_one_hot=date_features_to_one_hot,
                model=model,
                num_partitions=None,
            ),
            partition=partition_config,
            as_fugue=True,
        )
        return fa.get_native_as_df(result_df)

    def cross_validation(
        self,
        df: AnyDFType,
        h: _PositiveInt,
        freq: Optional[_Freq] = None,
        id_col: str = "unique_id",
        time_col: str = "ds",
        target_col: str = "y",
        level: Optional[list[Union[int, float]]] = None,
        quantiles: Optional[list[float]] = None,
        validate_api_key: bool = False,
        n_windows: _PositiveInt = 1,
        step_size: Optional[_PositiveInt] = None,
        finetune_steps: _NonNegativeInt = 0,
        finetune_depth: _FinetuneDepth = 1,
        finetune_loss: _Loss = "default",
        finetuned_model_id: Optional[str] = None,
        refit: bool = True,
        clean_ex_first: bool = True,
        hist_exog_list: Optional[list[str]] = None,
        date_features: Union[bool, list[str]] = False,
        date_features_to_one_hot: Union[bool, list[str]] = False,
        model: _Model = "timegpt-1",
        num_partitions: Optional[_PositiveInt] = None,
    ) -> AnyDFType:
        """Perform cross validation in your time series using TimeGPT.

        Parameters
        ----------
        df : pandas or polars DataFrame
            The DataFrame on which the function will operate. Expected to contain at least the following columns:
            - time_col:
                Column name in `df` that contains the time indices of the time series. This is typically a datetime
                column with regular intervals, e.g., hourly, daily, monthly data points.
            - target_col:
                Column name in `df` that contains the target variable of the time series, i.e., the variable we
                wish to predict or analyze.
            Additionally, you can pass multiple time series (stacked in the dataframe) considering an additional column:
            - id_col:
                Column name in `df` that identifies unique time series. Each unique value in this column
                corresponds to a unique time series.
        h : int
            Forecast horizon.
        freq : str, int or pandas offset, optional (default=None).
            Frequency of the timestamps. If `None`, it will be inferred automatically.
            See [pandas' available frequencies](https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#offset-aliases).
        id_col : str (default='unique_id')
            Column that identifies each series.
        time_col : str (default='ds')
            Column that identifies each timestep, its values can be timestamps or integers.
        target_col : str (default='y')
            Column that contains the target.
        level : float (default=99)
            Confidence level between 0 and 100 for prediction intervals.
        quantiles : list[float], optional (default=None)
            Quantiles to forecast, list between (0, 1).
            `level` and `quantiles` should not be used simultaneously.
            The output dataframe will have the quantile columns
            formatted as TimeGPT-q-(100 * q) for each q.
            100 * q represents percentiles but we choose this notation
            to avoid having dots in column names.
        validate_api_key : bool (default=False)
            If True, validates api_key before sending requests.
        n_windows : int (defaul=1)
            Number of windows to evaluate.
        step_size : int, optional (default=None)
            Step size between each cross validation window. If None it will be equal to `h`.
        finetune_steps : int (default=0)
            Number of steps used to finetune TimeGPT in the
            new data.
        finetune_depth : int (default=1)
            The depth of the finetuning. Uses a scale from 1 to 5, where 1 means little finetuning,
            and 5 means that the entire model is finetuned.
        finetune_loss : str (default='default')
            Loss function to use for finetuning. Options are: `default`, `mae`, `mse`, `rmse`, `mape`, and `smape`.
        finetuned_model_id : str, optional(default=None)
            ID of previously fine-tuned model to use.
        refit : bool (default=True)
            Fine-tune the model in each window. If `False`, only fine-tunes on the first window.
            Only used if `finetune_steps` > 0.
        clean_ex_first : bool (default=True)
            Clean exogenous signal before making forecasts using TimeGPT.
        hist_exog_list : list of str, optional (default=None)
            Column names of the historical exogenous features.
        date_features : bool or list of str or callable, optional (default=False)
            Features computed from the dates.
            Can be pandas date attributes or functions that will take the dates as input.
            If True automatically adds most used date features for the
            frequency of `df`.
        date_features_to_one_hot : bool or list of str (default=False)
            Apply one-hot encoding to these date features.
            If `date_features=True`, then all date features are
            one-hot encoded by default.
        model : str (default='timegpt-1')
            Model to use as a string. Options are: `timegpt-1`, and `timegpt-1-long-horizon`.
            We recommend using `timegpt-1-long-horizon` for forecasting
            if you want to predict more than one seasonal
            period given the frequency of your data.
        num_partitions : int (default=None)
            Number of partitions to use.
            If None, the number of partitions will be equal
            to the available parallel resources in distributed environments.

        Returns
        -------
        pandas, polars, dask or spark DataFrame or ray Dataset.
            DataFrame with cross validation forecasts.
        """
        if not isinstance(df, (pd.DataFrame, pl_DataFrame)):
            return self._distributed_cross_validation(
                df=df,
                h=h,
                freq=freq,
                id_col=id_col,
                time_col=time_col,
                target_col=target_col,
                level=level,
                quantiles=quantiles,
                n_windows=n_windows,
                step_size=step_size,
                validate_api_key=validate_api_key,
                finetune_steps=finetune_steps,
                finetune_depth=finetune_depth,
                finetune_loss=finetune_loss,
                finetuned_model_id=finetuned_model_id,
                refit=refit,
                clean_ex_first=clean_ex_first,
                hist_exog_list=hist_exog_list,
                date_features=date_features,
                date_features_to_one_hot=date_features_to_one_hot,
                model=model,
                num_partitions=num_partitions,
            )
        model = self._maybe_override_model(model)
        logger.info("Validating inputs...")
        df, _, drop_id, freq = self._run_validations(
            df=df,
            X_df=None,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            validate_api_key=validate_api_key,
            model=model,
            freq=freq,
        )
        level, quantiles = _prepare_level_and_quantiles(level, quantiles)
        if step_size is None:
            step_size = h

        logger.info("Preprocessing dataframes...")
        processed, _, x_cols, _ = _preprocess(
            df=df,
            X_df=None,
            h=0,
            freq=freq,
            date_features=date_features,
            date_features_to_one_hot=date_features_to_one_hot,
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
        )
        standard_freq = _standardize_freq(freq, processed)
        model_input_size, model_horizon = self._get_model_params(model, standard_freq)
        targets = _extract_target_array(df, target_col)
        times = df[time_col].to_numpy()
        if processed.sort_idxs is not None:
            targets = targets[processed.sort_idxs]
            times = times[processed.sort_idxs]
        restrict_input = finetune_steps == 0 and not x_cols
        if restrict_input:
            logger.info("Restricting input...")
            new_input_size = _restrict_input_samples(
                level=level,
                input_size=model_input_size,
                model_horizon=model_horizon,
                h=h,
            )
            new_input_size += h + step_size * (n_windows - 1)
            orig_indptr = processed.indptr
            processed = _tail(processed, new_input_size)
            times = _array_tails(times, orig_indptr, np.diff(processed.indptr))
            targets = _array_tails(targets, orig_indptr, np.diff(processed.indptr))
        X, hist_exog = _process_exog_features(processed.data, x_cols, hist_exog_list)

        logger.info("Calling Cross Validation Endpoint...")
        payload = {
            "series": {
                "y": targets,
                "sizes": np.diff(processed.indptr),
                "X": X,
            },
            "model": model,
            "h": h,
            "n_windows": n_windows,
            "step_size": step_size,
            "freq": standard_freq,
            "clean_ex_first": clean_ex_first,
            "hist_exog": hist_exog,
            "level": level,
            "finetune_steps": finetune_steps,
            "finetune_depth": finetune_depth,
            "finetune_loss": finetune_loss,
            "finetuned_model_id": finetuned_model_id,
            "refit": refit,
        }
        with self._make_client(**self._client_kwargs) as client:
            if num_partitions is None:
                resp = self._make_request_with_retries(
                    client, "v2/cross_validation", payload
                )
            else:
                payloads = _partition_series(payload, num_partitions, h=0)
                resp = self._make_partitioned_requests(
                    client, "v2/cross_validation", payloads
                )

        # assemble result
        idxs = np.array(resp["idxs"], dtype=np.int64)
        sizes = np.array(resp["sizes"], dtype=np.int64)
        window_starts = np.arange(0, sizes.sum(), h)
        cutoff_idxs = np.repeat(idxs[window_starts] - 1, h)
        out = type(df)(
            {
                id_col: ufp.repeat(processed.uids, sizes),
                time_col: times[idxs],
                "cutoff": times[cutoff_idxs],
                target_col: targets[idxs],
            }
        )
        out = ufp.assign_columns(out, "TimeGPT", resp["mean"])
        out = _maybe_add_intervals(out, resp["intervals"])
        out = _maybe_drop_id(df=out, id_col=id_col, drop=drop_id)
        return _maybe_convert_level_to_quantiles(out, quantiles)

    def plot(
        self,
        df: Optional[DataFrame] = None,
        forecasts_df: Optional[DataFrame] = None,
        id_col: str = "unique_id",
        time_col: str = "ds",
        target_col: str = "y",
        unique_ids: Union[Optional[list[str]], np.ndarray] = None,
        plot_random: bool = True,
        max_ids: int = 8,
        models: Optional[list[str]] = None,
        level: Optional[list[Union[int, float]]] = None,
        max_insample_length: Optional[int] = None,
        plot_anomalies: bool = False,
        engine: Literal["matplotlib", "plotly", "plotly-resampler"] = "matplotlib",
        resampler_kwargs: Optional[dict] = None,
        ax: Optional[
            Union["plt.Axes", np.ndarray, "plotly.graph_objects.Figure"]
        ] = None,
    ):
        """Plot forecasts and insample values.

        Parameters
        ----------
        df : pandas or polars DataFrame, optional (default=None)
            The DataFrame on which the function will operate. Expected to contain at least the following columns:
            - time_col:
                Column name in `df` that contains the time indices of the time series. This is typically a datetime
                column with regular intervals, e.g., hourly, daily, monthly data points.
            - target_col:
                Column name in `df` that contains the target variable of the time series, i.e., the variable we
                wish to predict or analyze.
            Additionally, you can pass multiple time series (stacked in the dataframe) considering an additional column:
            - id_col:
                Column name in `df` that identifies unique time series. Each unique value in this column
                corresponds to a unique time series.
        forecasts_df : pandas or polars DataFrame, optional (default=None)
            DataFrame with columns [`unique_id`, `ds`] and models.
        id_col : str (default='unique_id')
            Column that identifies each series.
        time_col : str (default='ds')
            Column that identifies each timestep, its values can be timestamps or integers.
        target_col : str (default='y')
            Column that contains the target.
        unique_ids : list[str], optional (default=None)
            Time Series to plot.
            If None, time series are selected randomly.
        plot_random : bool (default=True)
            Select time series to plot randomly.
        max_ids : int (default=8)
            Maximum number of ids to plot.
        models : list[str], optional (default=None)
            list of models to plot.
        level : list[float], optional (default=None)
            list of prediction intervals to plot if paseed.
        max_insample_length : int, optional (default=None)
            Max number of train/insample observations to be plotted.
        plot_anomalies : bool (default=False)
            Plot anomalies for each prediction interval.
        engine : str (default='matplotlib')
            Library used to plot. 'matplotlib', 'plotly' or 'plotly-resampler'.
        resampler_kwargs : dict
            Kwargs to be passed to plotly-resampler constructor.
            For further custumization ("show_dash") call the method,
            store the plotting object and add the extra arguments to
            its `show_dash` method.
        ax : matplotlib axes, array of matplotlib axes or plotly Figure, optional (default=None)
            Object where plots will be added.
        """
        try:
            from utilsforecast.plotting import plot_series
        except ModuleNotFoundError:
            raise Exception(
                "You have to install additional dependencies to use this method, "
                'please install them using `pip install "nixtla[plotting]"`'
            )
        if df is not None and id_col not in df.columns:
            df = ufp.copy_if_pandas(df, deep=False)
            df = ufp.assign_columns(df, id_col, "ts_0")
        df = ensure_time_dtype(df, time_col=time_col)
        if forecasts_df is not None:
            if id_col not in forecasts_df.columns:
                forecasts_df = ufp.copy_if_pandas(forecasts_df, deep=False)
                forecasts_df = ufp.assign_columns(forecasts_df, id_col, "ts_0")
            forecasts_df = ensure_time_dtype(forecasts_df, time_col=time_col)
            if "anomaly" in forecasts_df.columns:
                # special case to plot outputs
                # from detect_anomalies
                df = None
                forecasts_df = ufp.drop_columns(forecasts_df, "anomaly")
                cols = [
                    c.replace("TimeGPT-lo-", "")
                    for c in forecasts_df.columns
                    if "TimeGPT-lo-" in c
                ]
                level = [float(c) if "." in c else int(c) for c in cols]
                plot_anomalies = True
                models = ["TimeGPT"]
        return plot_series(
            df=df,
            forecasts_df=forecasts_df,
            ids=unique_ids,
            plot_random=plot_random,
            max_ids=max_ids,
            models=models,
            level=level,
            max_insample_length=max_insample_length,
            plot_anomalies=plot_anomalies,
            engine=engine,
            resampler_kwargs=resampler_kwargs,
            palette="tab20b",
            id_col=id_col,
            time_col=time_col,
            target_col=target_col,
            ax=ax,
        )

    @staticmethod
    def audit_data(
        df: AnyDFType,
        freq: _Freq,
        id_col: str = "unique_id",
        time_col: str = "ds",
        target_col: str = "y",
        start: Union[str, int, datetime.date, datetime.datetime] = "per_serie",
        end: Union[str, int, datetime.date, datetime.datetime] = "global",
    ) -> tuple[bool, dict[str, DataFrame], dict[str, DataFrame]]:
        """Audit data quality.

        Parameters
        ----------
        df : pandas or polars DataFrame
            The dataframe to be audited.
        freq : str, int or pandas offset.
            Frequency of the timestamps. Must be specified.
            See [pandas' available frequencies](https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#offset-aliases).
        id_col : str
            Column that identifies each series, by default 'unique_id'
        time_col : str
            Column that identifies each timestep, its values can be timestamps or
            integers, by default 'ds'
        target_col : str
            Column that contains the target, by default 'y'
        start : Union[str, int, datetime.date, datetime.datetime], optional
            Initial timestamp for the series.
                * 'per_serie' uses each series first timestamp
                * 'global' uses the first timestamp seen in the data
                * Can also be a specific timestamp or integer,
                e.g. '2000-01-01', 2000 or datetime(2000, 1, 1)
            , by default "per_serie"
        end : Union[str, int, datetime.date, datetime.datetime], optional
            Final timestamp for the series.
                * 'per_serie' uses each series last timestamp
                * 'global' uses the last timestamp seen in the data
                * Can also be a specific timestamp or integer,
                e.g. '2000-01-01', 2000 or datetime(2000, 1, 1)
            , by default "global"

        Returns
        -------
        tuple[bool, dict[str, DataFrame], dict[str, DataFrame]]
            Tuple containing:
            - bool: True if all tests pass, False otherwise
            - dict: Dictionary mapping test IDs to error DataFrames for failed
                    tests or None if the test could not be performed.
            - dict: Dictionary mapping test IDs to error DataFrames for
                    case-specific tests.

            Test IDs:
            - D001: Test for duplicate rows
            - D002: Test for missing dates
            - F001: Test for presence of categorical feature columns
            - V001: Test for negative values
            - V002: Test for leading zeros

        """
        df = ensure_time_dtype(df, time_col=time_col)

        logger.info("Running data quality tests...")
        pass_D001, error_df_D001 = _audit_duplicate_rows(df, id_col, time_col)
        pass_D002, error_df_D002 = AuditDataSeverity.FAIL, None
        if pass_D001 != AuditDataSeverity.FAIL:
            # If data has duplicate rows, missing dates can not be added by fill_gaps.
            # Duplicate rows issue needs to be resolved first.
            pass_D002, error_df_D002 = _audit_missing_dates(
                df, freq, id_col, time_col, start, end
            )
        pass_F001, error_df_F001 = _audit_categorical_variables(df, id_col, time_col)
        pass_V001, error_df_V001 = _audit_negative_values(df, target_col)
        pass_V002, error_df_V002 = _audit_leading_zeros(
            df, id_col, time_col, target_col
        )

        fail_dict, case_specific_dict = {}, {}
        test_ids = ["D001", "D002", "F001", "V001", "V002"]
        pass_vals = [pass_D001, pass_D002, pass_F001, pass_V001, pass_V002]
        error_dfs = [
            error_df_D001,
            error_df_D002,
            error_df_F001,
            error_df_V001,
            error_df_V002,
        ]
        all_pass = True

        for test_id, pass_val, error_df in zip(test_ids, pass_vals, error_dfs):
            # Only include errors for failed or case specific tests
            if pass_val == AuditDataSeverity.FAIL:
                all_pass = False
                if error_df is not None:
                    logger.warning(
                        f"Failure {test_id} detected with critical severity."
                    )
                else:
                    logger.warning(f"Test {test_id} could not be performed.")
                fail_dict[test_id] = error_df

            if pass_val == AuditDataSeverity.CASE_SPECIFIC:
                all_pass = False
                logger.warning(
                    f"Failure {test_id} detected which could cause issue depending on the use case."
                )
                case_specific_dict[test_id] = error_df

        if all_pass:
            logger.info("All checks passed...")
        return all_pass, fail_dict, case_specific_dict

    def clean_data(
        self,
        df: AnyDFType,
        fail_dict: dict[str, DataFrame],
        case_specific_dict: dict[str, DataFrame],
        freq: _Freq,
        id_col: str = "unique_id",
        time_col: str = "ds",
        target_col: str = "y",
        clean_case_specific: bool = False,
        agg_dict: Optional[dict[str, Union[str, Callable]]] = None,
    ) -> tuple[AnyDFType, bool, dict[str, DataFrame], dict[str, DataFrame]]:
        """Clean the data. This should be run after running `audit_data`.

        Parameters
        ----------
        df : AnyDFType
            The dataframe to be cleaned
        fail_dict : dict[str, DataFrame]
            The failure dictionary from the audit_data method
        case_specific_dict : dict[str, DataFrame]
            The case specific dictionary from the audit_data method
        freq : str, int or pandas offset.
            Frequency of the timestamps. Must be specified.
            See [pandas' available frequencies](https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#offset-aliases).
        id_col : str
            Column that identifies each series, by default 'unique_id'
        time_col : str
            Column that identifies each timestep, its values can be timestamps or
            integers, by default 'ds'
        target_col : str
            Column that contains the target, by default 'y'
        clean_case_specific : bool, optional
            If True, clean case specific issues, by default False
        agg_dict : Optional[dict[str, Union[str, Callable]]], optional
            The aggregation methods to use when there are duplicate rows (D001),
            by default None

        Returns
        -------
        tuple[AnyDFType, bool, dict[str, DataFrame], dict[str, DataFrame]]
            Tuple containing:
            - AnyDFType: The cleaned dataframe
            - The three outputs from audit_data that are run at the end of cleansing.

        Raises
        ------
        ValueError
            Any exceptions during the cleaning process.
        """
        df = ensure_time_dtype(df, time_col=time_col)
        logger.info("Running data cleansing...")

        if fail_dict:
            if "D001" in fail_dict:
                try:
                    logger.info("Fixing D001: Cleaning duplicate rows...")

                    if agg_dict is None:
                        raise ValueError(
                            "agg_dict must be provided to resolve D001 failure."
                        )

                    # Get all columns except id_col and time_col
                    other_cols = [
                        col for col in df.columns if col not in [id_col, time_col]
                    ]

                    # Verify all columns have aggregation rules
                    missing_cols = [col for col in other_cols if col not in agg_dict]
                    if missing_cols:
                        raise ValueError(
                            f"D001: Missing aggregation rules for columns: {missing_cols}. "
                            "Please provide aggregation rules for all columns in agg_dict."
                        )
                    df = df.groupby([id_col, time_col], as_index=False).agg(agg_dict)
                except Exception as e:
                    raise ValueError(f"Error cleaning duplicate rows D001: {e}")
            if "D002" in fail_dict:
                try:
                    missing = fail_dict.get("D002")
                    if missing is None:
                        logger.warning(
                            "D002: Missing dates could not be checked by audit_data. "
                            "Hence not filling missing dates..."
                        )
                    else:
                        logger.info("Fixing D002: Filling missing dates...")
                        df = pd.concat([df, fail_dict.get("D002")])
                except Exception as e:
                    raise ValueError(f"Error filling missing dates D002: {e}")

        if case_specific_dict and clean_case_specific:
            if "V001" in case_specific_dict:
                try:
                    logger.info("Fixing V001: Removing negative values...")
                    df.loc[df[target_col] < 0, target_col] = 0
                except Exception as e:
                    raise ValueError(f"Error removing negative values V001: {e}")

            if "V002" in case_specific_dict:
                try:
                    logger.info("Fixing V002: Removing leading zeros...")
                    leading_zeros_df = case_specific_dict["V002"]
                    leading_zeros_dict = leading_zeros_df.set_index(id_col)[
                        "first_nonzero_index"
                    ].to_dict()
                    df = df.groupby(id_col, group_keys=False).apply(
                        lambda group: group.loc[
                            group.index
                            >= leading_zeros_dict.get(group.name, group.index[0])
                        ]
                    )
                except Exception as e:
                    raise ValueError(f"Error removing leading zeros V002: {e}")

        # Run data quality checks on the cleaned data
        all_pass, error_dfs, case_specific_dfs = self.audit_data(
            df=df, freq=freq, id_col=id_col, time_col=time_col
        )

        return df, all_pass, error_dfs, case_specific_dfs

def _forecast_wrapper(
    df: pd.DataFrame,
    client: NixtlaClient,
    h: _PositiveInt,
    freq: Optional[_Freq],
    id_col: str,
    time_col: str,
    target_col: str,
    level: Optional[list[Union[int, float]]],
    quantiles: Optional[list[float]],
    finetune_steps: _NonNegativeInt,
    finetune_depth: _FinetuneDepth,
    finetune_loss: _Loss,
    finetuned_model_id: Optional[str],
    clean_ex_first: bool,
    hist_exog_list: Optional[list[str]],
    validate_api_key: bool,
    add_history: bool,
    date_features: Union[bool, list[Union[str, Callable]]],
    date_features_to_one_hot: Union[bool, list[str]],
    model: _Model,
    num_partitions: Optional[_PositiveInt],
    feature_contributions: bool,
) -> pd.DataFrame:
    if "_in_sample" in df:
        in_sample_mask = df["_in_sample"]
        X_df = df.loc[~in_sample_mask].drop(columns=["_in_sample", target_col])
        df = df.loc[in_sample_mask].drop(columns="_in_sample")
    else:
        X_df = None
    return client.forecast(
        df=df,
        h=h,
        freq=freq,
        id_col=id_col,
        time_col=time_col,
        target_col=target_col,
        X_df=X_df,
        level=level,
        quantiles=quantiles,
        finetune_steps=finetune_steps,
        finetune_depth=finetune_depth,
        finetune_loss=finetune_loss,
        finetuned_model_id=finetuned_model_id,
        clean_ex_first=clean_ex_first,
        hist_exog_list=hist_exog_list,
        validate_api_key=validate_api_key,
        add_history=add_history,
        date_features=date_features,
        date_features_to_one_hot=date_features_to_one_hot,
        model=model,
        num_partitions=num_partitions,
        feature_contributions=feature_contributions,
    )


def _detect_anomalies_wrapper(
    df: pd.DataFrame,
    client: NixtlaClient,
    freq: Optional[_Freq],
    id_col: str,
    time_col: str,
    target_col: str,
    level: Union[int, float],
    finetuned_model_id: Optional[str],
    clean_ex_first: bool,
    validate_api_key: bool,
    date_features: Union[bool, list[str]],
    date_features_to_one_hot: Union[bool, list[str]],
    model: _Model,
    num_partitions: Optional[_PositiveInt],
) -> pd.DataFrame:
    return client.detect_anomalies(
        df=df,
        freq=freq,
        id_col=id_col,
        time_col=time_col,
        target_col=target_col,
        level=level,
        finetuned_model_id=finetuned_model_id,
        clean_ex_first=clean_ex_first,
        validate_api_key=validate_api_key,
        date_features=date_features,
        date_features_to_one_hot=date_features_to_one_hot,
        model=model,
        num_partitions=num_partitions,
    )


def _detect_anomalies_online_wrapper(
    df: pd.DataFrame,
    client: NixtlaClient,
    h: _PositiveInt,
    detection_size: _PositiveInt,
    threshold_method: _ThresholdMethod,
    freq: Optional[_Freq],
    id_col: str,
    time_col: str,
    target_col: str,
    level: Union[int, float],
    clean_ex_first: bool,
    step_size: _PositiveInt,
    finetune_steps: _NonNegativeInt,
    finetune_depth: _FinetuneDepth,
    finetune_loss: _Loss,
    hist_exog_list: Optional[list[str]],
    date_features: Union[bool, list[str]],
    date_features_to_one_hot: Union[bool, list[str]],
    model: _Model,
    refit: bool,
    num_partitions: Optional[_PositiveInt],
) -> pd.DataFrame:
    return client.detect_anomalies_online(
        df=df,
        h=h,
        detection_size=detection_size,
        threshold_method=threshold_method,
        freq=freq,
        id_col=id_col,
        time_col=time_col,
        target_col=target_col,
        level=level,
        clean_ex_first=clean_ex_first,
        step_size=step_size,
        finetune_steps=finetune_steps,
        finetune_depth=finetune_depth,
        finetune_loss=finetune_loss,
        hist_exog_list=hist_exog_list,
        date_features=date_features,
        date_features_to_one_hot=date_features_to_one_hot,
        model=model,
        refit=refit,
        num_partitions=num_partitions,
    )


def _cross_validation_wrapper(
    df: pd.DataFrame,
    client: NixtlaClient,
    h: _PositiveInt,
    freq: Optional[_Freq],
    id_col: str,
    time_col: str,
    target_col: str,
    level: Optional[list[Union[int, float]]],
    quantiles: Optional[list[float]],
    validate_api_key: bool,
    n_windows: _PositiveInt,
    step_size: Optional[_PositiveInt],
    finetune_steps: _NonNegativeInt,
    finetune_depth: _FinetuneDepth,
    finetune_loss: _Loss,
    finetuned_model_id: Optional[str],
    refit: bool,
    clean_ex_first: bool,
    hist_exog_list: Optional[list[str]],
    date_features: Union[bool, list[str]],
    date_features_to_one_hot: Union[bool, list[str]],
    model: _Model,
    num_partitions: Optional[_PositiveInt],
) -> pd.DataFrame:
    return client.cross_validation(
        df=df,
        h=h,
        freq=freq,
        id_col=id_col,
        time_col=time_col,
        target_col=target_col,
        level=level,
        quantiles=quantiles,
        validate_api_key=validate_api_key,
        n_windows=n_windows,
        step_size=step_size,
        finetune_steps=finetune_steps,
        finetune_depth=finetune_depth,
        finetune_loss=finetune_loss,
        finetuned_model_id=finetuned_model_id,
        refit=refit,
        clean_ex_first=clean_ex_first,
        hist_exog_list=hist_exog_list,
        date_features=date_features,
        date_features_to_one_hot=date_features_to_one_hot,
        model=model,
        num_partitions=num_partitions,
    )


def _get_schema(
    df: "AnyDataFrame",
    method: str,
    id_col: str,
    time_col: str,
    target_col: str,
    level: Optional[Union[int, float, list[Union[int, float]]]],
    quantiles: Optional[list[float]],
) -> "triad.Schema":
    import fugue.api as fa

    base_cols = [id_col, time_col]
    if method != "forecast":
        base_cols.append(target_col)
    schema = fa.get_schema(df).extract(base_cols).copy()
    schema.append("TimeGPT:double")
    if method == "detect_anomalies":
        schema.append("anomaly:bool")
    if method == "detect_anomalies_online":
        schema.append("anomaly:bool")
        schema.append("anomaly_score:double")
    elif method == "cross_validation":
        schema.append(("cutoff", schema[time_col].type))
    if level is not None and quantiles is not None:
        raise ValueError("You should provide `level` or `quantiles` but not both.")
    if level is not None:
        if not isinstance(level, list):
            level = [level]
        level = sorted(level)
        schema.append(",".join(f"TimeGPT-lo-{lv}:double" for lv in reversed(level)))
        schema.append(",".join(f"TimeGPT-hi-{lv}:double" for lv in level))
    if quantiles is not None:
        quantiles = sorted(quantiles)
        q_cols = [f"TimeGPT-q-{int(q * 100)}:double" for q in quantiles]
        schema.append(",".join(q_cols))
    return schema


def _distributed_setup(
    df: "AnyDataFrame",
    method: str,
    id_col: str,
    time_col: str,
    target_col: str,
    level: Optional[Union[int, float, list[Union[int, float]]]],
    quantiles: Optional[list[float]],
    num_partitions: Optional[int],
) -> tuple["triad.Schema", dict[str, Any]]:
    from fugue.execution import infer_execution_engine

    if infer_execution_engine([df]) is None:
        raise ValueError(
            f"Could not infer execution engine for type {type(df).__name__}. "
            "Expected a spark or dask DataFrame or a ray Dataset."
        )
    schema = _get_schema(
        df=df,
        method=method,
        id_col=id_col,
        time_col=time_col,
        target_col=target_col,
        level=level,
        quantiles=quantiles,
    )
    partition_config: dict[str, Any] = dict(by=id_col, algo="coarse")
    if num_partitions is not None:
        partition_config["num"] = num_partitions
    return schema, partition_config
