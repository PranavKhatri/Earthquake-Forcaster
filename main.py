from flask import Flask, render_template, flash, request
import logging, io, base64, os, datetime
from datetime import datetime
from datetime import timedelta
import pandas as pd
import numpy as np
import xgboost as xgb

# global variables
earthquake_live = None
days_out_to_predict = 5


app = Flask(__name__)

def prepare_earthquake_data_and_model(days_out_to_predict = 5, max_depth=3, eta=0.1):
    import pandas as pd
    import numpy as np
    # get latest data from USGS servers
    df = pd.read_csv('https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_month.csv')
    df = df.sort_values('time', ascending=True)
    # truncate time from datetime
    df['date'] = df['time'].str[0:10]

    # only keep the columns needed
    df = df[['date', 'latitude', 'longitude', 'depth', 'mag', 'place']]
    temp_df = df['place'].str.split(', ', expand=True) 
    df['place'] = temp_df[1]
    df = df[['date', 'latitude', 'longitude', 'depth', 'mag', 'place']]

    # calculate mean lat lon for simplified locations
    df_coords = df[['place', 'latitude', 'longitude']]
    df_coords = df_coords.groupby(['place'], as_index=False).mean()
    df_coords = df_coords[['place', 'latitude', 'longitude']]

    df = df[['date', 'depth', 'mag', 'place']]
    df = pd.merge(left=df, right=df_coords, how='inner', on=['place'])

    # loop through each zone and apply MA
    eq_data = []
    df_live = []
    for symbol in list(set(df['place'])):
        temp_df = df[df['place'] == symbol].copy()
        temp_df['depth_avg_20'] = temp_df['depth'].rolling(window=20,center=False).mean() 
        temp_df['depth_avg_10'] = temp_df['depth'].rolling(window=10,center=False).mean()
        temp_df['depth_avg_5'] = temp_df['depth'].rolling(window=5,center=False).mean()
        temp_df['mag_avg_20'] = temp_df['mag'].rolling(window=20,center=False).mean() 
        temp_df['mag_avg_10'] = temp_df['mag'].rolling(window=10,center=False).mean()
        temp_df['mag_avg_5'] = temp_df['mag'].rolling(window=5,center=False).mean()
        temp_df.loc[:, 'mag_outcome'] = temp_df.loc[:, 'mag_avg_5'].shift(days_out_to_predict * -1)

        df_live.append(temp_df.tail(days_out_to_predict))

        eq_data.append(temp_df)

    # concat all location-based dataframes into master dataframe
    df = pd.concat(eq_data)

    # remove any NaN fields
    df = df[np.isfinite(df['depth_avg_20'])]
    df = df[np.isfinite(df['mag_avg_20'])]
    df = df[np.isfinite(df['mag_outcome'])]

    # prepare outcome variable
    df['mag_outcome'] = np.where(df['mag_outcome'] > 2.5, 1,0)

    df = df[['date',
             'latitude',
             'longitude',
             'depth_avg_20',
             'depth_avg_10',
             'depth_avg_5',
             'mag_avg_20', 
             'mag_avg_10',
             'mag_avg_5',
             'mag_outcome']]

    # keep only data where we can make predictions
    df_live = pd.concat(df_live)
    df_live = df_live[np.isfinite(df_live['mag_avg_20'])]

    # let's train the model whenever the webserver is restarted
    from sklearn.model_selection import train_test_split
    features = [f for f in list(df) if f not in ['date', 'mag_outcome', 'latitude',
     'longitude']]

    X_train, X_test, y_train, y_test = train_test_split(df[features],
                         df['mag_outcome'], test_size=0.3, random_state=42)
    import xgboost as xgb
    dtrain = xgb.DMatrix(X_train[features], label=y_train)
    dtest = xgb.DMatrix(X_test[features], label=y_test)

    param = {
            'objective': 'binary:logistic',
            'booster': 'gbtree',
            'eval_metric': 'auc',
            'max_depth': max_depth,  # the maximum depth of each tree
            'eta': eta,  # the training step for each iteration
            'silent': 1}  # logging mode - quiet}  # the number of classes that exist in this datset

    num_round = 500  # the number of training iterations    
    early_stopping_rounds=30
    xgb_model = xgb.train(param, dtrain, num_round) 


    # train on live data
    dlive = xgb.DMatrix(df_live[features])  
    preds = xgb_model.predict(dlive)

    # add preds to live data
    df_live = df_live[['date', 'place', 'latitude', 'longitude']]
    # add predictions back to dataset 
    df_live = df_live.assign(preds=pd.Series(preds).values)

    # aggregate down dups
    df_live = df_live.groupby(['date', 'place'], as_index=False).mean()

    # increment date to include DAYS_OUT_TO_PREDICT
    df_live['date']= pd.to_datetime(df_live['date'],format='%Y-%m-%d') 
    df_live['date'] = df_live['date'] + pd.to_timedelta(days_out_to_predict,unit='d')

    return(df_live)

def get_earth_quake_estimates(desired_date, df_live):
    from datetime import datetime
    live_set_tmp = df_live[df_live['date'] == desired_date]

    # format lat/lons like Google Maps expects
    LatLngString = ''
    if (len(live_set_tmp) > 0):
        for lat, lon, pred in zip(live_set_tmp['latitude'], live_set_tmp['longitude'], live_set_tmp['preds']): 
            # this is the threashold of probability to decide what to show and what not to show
            if (pred > 0.0):
                LatLngString += "new google.maps.LatLng(" + str(lat) + "," + str(lon) + "),"

    return(LatLngString)


@app.before_first_request
def startup():
    global earthquake_live

    # prepare earthquake data, model and get live data set with earthquake forecasts
    earthquake_live = prepare_earthquake_data_and_model()


@app.route("/", methods=['POST', 'GET'])
def build_page():
        if request.method == 'POST':

            horizon_int = int(request.form.get('slider_date_horizon'))
            horizon_date = datetime.today() + timedelta(days=horizon_int)

            return render_template('index.html',
                date_horizon = horizon_date.strftime('%m/%d/%Y'),
                earthquake_horizon = get_earth_quake_estimates(str(horizon_date)[:10], earthquake_live),
                current_value=horizon_int, 
                days_out_to_predict=days_out_to_predict)

        else:
            # set blank map
            return render_template('index.html',
                date_horizon = datetime.today().strftime('%m/%d/%Y'),
                earthquake_horizon = '',
                current_value=0,
                days_out_to_predict=days_out_to_predict)



if __name__=='__main__':
    app.run(debug=True)
