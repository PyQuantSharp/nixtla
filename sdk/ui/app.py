from itertools import product
from pathlib import Path
import random

from autotimeseries.core import AutoTS
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


st.image('https://raw.githubusercontent.com/Nixtla/nixtlats/master/nbs/indx_imgs/nixtla_logo.png')
st.sidebar.image('https://avatars.githubusercontent.com/u/79945230?s=400&u=6d3dd56a7957fb80a719bad86b29e3feb0e3a7a6&v=4')

"""
# Time Series Forecasting at Scale by Nixtla

:fire::fire::fire: With this App you can  easly build you own forecasts at scale in under 5 minutes leveragin the power of [Nixtla](https://github.com/Nixtla/nixtla) :fire::fire::fire:

You can use our fully hosted version as a service through this App or through [python SDK](https://github.com/Nixtla/nixtla/tree/main/sdk/) ([autotimeseries](https://pypi.org/project/autotimeseries/)). 
To consume the APIs on our own infrastructure just request tokens by sending an email to federico@nixtla.io or opening a GitHub issue. 
**We currently have free resources available for anyone interested.**

"""

def plot_grid_prediction(y, plot_random=True, unique_ids=None):
    """
    y: pandas df
        panel with columns unique_id, ds, y
    plot_random: bool
        if unique_ids will be sampled
    unique_ids: list
        unique_ids to plot
    """
    pd.plotting.register_matplotlib_converters()

    fig, axes = plt.subplots(4, 1, figsize = (16, 24))

    if not unique_ids:
        unique_ids = y['unique_id'].unique()

    assert len(unique_ids) >= 4, "Must provide at least 4 ts"

    if plot_random:
        unique_ids = random.choices(unique_ids, k=4)

    for i, (idx, idy) in enumerate(product(range(4), range(1))):
        y_uid = y[y.unique_id == unique_ids[i]]
        #y_uid_hat = y_hat[y_hat.unique_id == unique_ids[i]]

        axes[idx].plot(y_uid.ds, y_uid.y_pred, label = 'y', marker='.')
        plt.subplots_adjust(hspace = 1)
        #for model in models:
        #    axes[idx].plot(y_uid_hat.ds, y_uid_hat[model], label=model, marker='.')
        axes[idx].set_title(unique_ids[i])
        axes[idx].legend(loc='upper left')
        axes[idx].tick_params(axis='x', rotation=90, length=5)

    return fig

def read_forecasts(autotimeseries):
    save_dest = Path('downloads')
    save_dest.mkdir(exist_ok=True)
    filename_output = str(save_dest / 'forecasts.csv')

    autotimeseries.download_from_s3(filename='forecasts_2021-10-12_21-50-08.csv',
                                    filename_output=filename_output)

    return filename_output

def read_benchmarks(autotimeseries):
    save_dest = Path('downloads')
    save_dest.mkdir(exist_ok=True)
    filename_output = str(save_dest / 'benchmarks.csv')

    autotimeseries.download_from_s3(filename='benchmarks.csv',
                                    filename_output=filename_output)

    return filename_output

def main():
    API_KEY = st.sidebar.text_input('Enter API_KEY', type='password')
    
    BUCKET_NAME = st.secrets['BUCKET_NAME'] 
    API_ID = st.secrets['API_ID']
    AWS_ACCESS_KEY_ID = st.secrets['AWS_ACCESS_KEY_ID']
    AWS_SECRET_ACCESS_KEY = st.secrets['AWS_SECRET_ACCESS_KEY']

    autotimeseries = AutoTS(bucket_name=BUCKET_NAME,
                            api_id=API_ID, 
                            api_key=API_KEY,
                            aws_access_key_id=AWS_ACCESS_KEY_ID, 
                            aws_secret_access_key=AWS_SECRET_ACCESS_KEY)
    
    st.session_state.file_forecast = read_forecasts(autotimeseries)
    st.session_state.file_benchmark = read_benchmarks(autotimeseries)

    st.subheader('Forecast your data')

    filename_target = st.file_uploader('Enter target file', 
                                       help='Local file with the time series you want to forecast')
    filename_temporal = st.file_uploader('Enter temporal exogenous file',
                                         help='Local file with temporal exogenous variables')
    filename_static = st.file_uploader('Enter static exogenous file',
                                       help='Local file wih static exogenous variables')

    unique_id_column = st.text_input('Enter unique_id column', value='item_id')
    ds_column = st.text_input('Enter date column', value='timestamp')
    y_column = st.text_input('Enter target column', value='demand')

    columns = dict(unique_id_column=unique_id_column,
                   ds_column=ds_column,
                   y_column=y_column)

    freq = st.text_input('Enter frequency of your data', value='D')
    horizon = st.text_input('Enter horizon to forecast', value=28)

    add_calendar = st.checkbox('Add calendar variables')
    if add_calendar:
        st.text_input('Enter country', value='USA')

    if st.button('Forecast'):
        with st.spinner('Uploading data'):
            filename_target_u = autotimeseries.upload_to_s3(filename_target.name)
            filename_temporal_u = autotimeseries.upload_to_s3(filename_temporal.name)
            filename_static_u = autotimeseries.upload_to_s3(filename_static.name)
        
        with st.spinner('Calling Nixtla API'):
            response_forecast = autotimeseries.tsforecast(filename_target=filename_target_u,
                                                          freq=freq,
                                                          horizon=horizon, 
                                                          filename_static=filename_static_u,
                                                          filename_temporal=filename_temporal_u,
                                                          objective='tweedie',
                                                          metric='rmse',
                                                          n_estimators=170,
                                                          **columns)
            st.session_state.id_job = response_forecast['id_job']

        st.write(response_forecast)

    st.subheader('Get status')
    st.write('Check the progress of your job')
    if st.button('Get status'):
        with st.spinner('Calling Nixtla API'):
            status = autotimeseries.get_status(st.session_state.id_job)
        st.write(status)

    st.subheader('Download results')
    st.write('Get your forecasts in csv format')
    if st.session_state.file_forecast == '':
        forecast = pd.DataFrame()
    else:
        forecast = pd.read_csv(st.session_state.file_forecast)
    #st.dataframe(forecast)
    st.download_button('Download data', 
                       data=forecast.to_csv(),
                       file_name='forecast.csv')


    st.subheader('Benchmark forecasts')
    st.write('Compare your forecasts against other solutions')
    
    if st.button('Benchmark'):
        if st.session_state.file_benchmark == '':
            benchmark = pd.DataFrame()
        else:
            benchmark = pd.read_csv(st.session_state.file_benchmark)
        st.dataframe(benchmark)

    st.subheader('Plot forecasts')

    if st.button('Plot forecasts'):
        forecast = pd.read_csv('downloads/forecasts.csv')
        fig = plot_grid_prediction(forecast)
        st.pyplot(fig)

if __name__ == '__main__':
    main()
