__all__ = ['CountryHolidays', 'SpecialDates']

import pandas as pd

def _transform_dict_holidays(dict_holidays_dates):
    dict_holidays = {}
    for key, value in dict_holidays_dates.items():
        if value not in dict_holidays:
            dict_holidays[value] = []
        dict_holidays[value].append(key)
    return dict_holidays

def _get_holidays_df(dates, categories, holiday_extractor, supported_categories):
    years = dates.year.unique().tolist()
    total_holidays = dict()
    for cat in categories:
        if cat not in supported_categories:
            raise Exception(f"Holidays for {cat} not available, please remove it.")
        dict_holidays = _transform_dict_holidays(holiday_extractor(cat, years=years))
        for key, val in dict_holidays.items():
            total_holidays[f"{cat}_{key}"] = [int(ds.date() in val) for ds in dates]
    return pd.DataFrame(total_holidays, index=dates)

class CountryHolidays:
    """Given a list of countries, returns a dataframe with holidays for each country."""

    def __init__(self, countries: list[str]):
        self.countries = countries

    def __call__(self, dates: pd.DatetimeIndex):
        try:
            from holidays.utils import country_holidays
            from holidays.utils import list_supported_countries
        except ModuleNotFoundError:
            raise Exception(
                "You have to install additional libraries to use holidays, "
                'please install them using `pip install "nixtla[date_extras]"`'
            )
        return _get_holidays_df(
            dates, self.countries, country_holidays, list_supported_countries()
        )

    def __name__(self):
        return "CountryHolidays"

class SpecialDates:
    """Given a dictionary of categories and dates, returns a dataframe with the special dates."""

    def __init__(self, special_dates: dict[str, list[str]]):
        self.special_dates = special_dates

    def __call__(self, dates: pd.DatetimeIndex):
        total_special_dates = dict()
        for key, val in self.special_dates.items():
            date_vals = [ds.date() for ds in pd.to_datetime(val)]
            total_special_dates[key] = [int(ds.date() in date_vals) for ds in dates]
        return pd.DataFrame(total_special_dates, index=dates)

    def __name__(self):
        return "SpecialDates"
