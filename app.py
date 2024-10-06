from flask import Flask, request, jsonify
import pandas as pd
import numpy as np
import warnings
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler
from flask_caching import Cache
import os

# Suppress all warnings
warnings.filterwarnings('ignore')

# Flask app
app = Flask(__name__)
cache = Cache(app, config={'CACHE_TYPE': 'simple'})

# Global variables to keep track of the dataframe and file path
global_df = None
current_filepath = None

# The ExoData class
class ExoData():
    def __init__(self, path):
        # Read the CSV file and perform initial transformations
        self.exoplanet_data = pd.read_csv(path, comment='#', delimiter=',')
        self.exoplanet_data = self.precompute_columns(self.exoplanet_data)

    def precompute_columns(self, df):
        # Validate schema
        self.validate_schema(df)
        
        # Extract the relevant columns
        columns_to_include = [
            'pl_name', 'hostname', 'ra', 'dec', 'sy_dist', 'st_rad', 'st_teff', 'pl_orbsmax', 'pl_orbeccen',
            'pl_orbincl', 'pl_rade', 'pl_eqt', 'pl_orbper', 'st_lum', 'sy_snum', 'disc_year'
        ]
        df = df[columns_to_include].copy()

        # Fill NaN values for numeric columns with their median
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        df[numeric_cols] = df[numeric_cols].apply(lambda x: pd.to_numeric(x, downcast='float').fillna(x.median()))

        # Fill NaN values for non-numeric columns with the placeholder 'unknown'
        non_numeric_cols = df.select_dtypes(exclude=[np.number]).columns
        df[non_numeric_cols] = df[non_numeric_cols].fillna('unknown')

        # Convert RA and Dec from degrees to radians
        df['ra_rad'] = np.radians(df['ra'])
        df['dec_rad'] = np.radians(df['dec'])

        # Calculate Cartesian coordinates (X, Y, Z)
        df['X'] = df['sy_dist'] * np.cos(df['ra_rad']) * np.cos(df['dec_rad'])
        df['Y'] = df['sy_dist'] * np.sin(df['ra_rad']) * np.cos(df['dec_rad'])
        df['Z'] = df['sy_dist'] * np.sin(df['dec_rad'])

        # Calculate habitable zone boundaries
        df['habitable_zone_inner'] = np.sqrt(df['st_lum'] / 1.1)
        df['habitable_zone_outer'] = np.sqrt(df['st_lum'] / 0.53)

        # Habitability metrics
        df['habitable_zone_center_au'] = (df['habitable_zone_inner'] + df['habitable_zone_outer']) / 2
        df['habitable_zone_width_au'] = (df['habitable_zone_outer'] - df['habitable_zone_inner']) / 2
        df['hz_score'] = 1 - abs((df['pl_orbsmax'] - df['habitable_zone_center_au']) / df['habitable_zone_width_au'])
        df['hz_score'] = df['hz_score'].clip(lower=0)

        # Other scores
        df['size_score'] = np.exp(-((df['pl_rade'] - 1) ** 2) / 2)
        df['temp_score'] = np.exp(-((df['pl_eqt'] - 300) ** 2) / 2000)
        df['eccentricity_score'] = 1 - df['pl_orbeccen']
        df['eccentricity_score'] = df['eccentricity_score'].clip(lower=0)

        # Overall habitability score
        df['habitability_score'] = (df['hz_score'] *
                                    df['size_score'] *
                                    df['temp_score'] *
                                    df['eccentricity_score'])

        return df

    @staticmethod
    def validate_schema(df):
        required_columns = {'pl_name', 'hostname', 'ra', 'dec', 'sy_dist', 'st_rad', 'st_teff', 'pl_orbsmax', 'pl_orbeccen', 'pl_orbincl', 'pl_rade', 'pl_eqt', 'pl_orbper', 'st_lum', 'sy_snum', 'disc_year'}
        if not required_columns.issubset(df.columns):
            raise ValueError("The provided file does not contain the required columns.")

# Helper function to load global DataFrame
def load_global_dataframe(filepath):
    global global_df, current_filepath
    if global_df is None or current_filepath != filepath:
        exo_data_instance = ExoData(filepath)
        global_df = exo_data_instance.precompute_columns(exo_data_instance.exoplanet_data)
        current_filepath = filepath

@app.before_request
def before_request_func():
    data = request.json
    filepath = data.get('filepath', 'PSCompPars.csv')
    load_global_dataframe(filepath)

# Function to calculate SNR dynamically
def calculate_snr(df, SNR0, D):
    df['snr'] = SNR0 * ((df['st_rad'] * df['pl_rade'] * (D / 6)) /
                        ((df['sy_dist'] / 10) * df['pl_orbsmax'])) ** 2
    df['habitable'] = df['snr'] > 5
    return df

# Route to get top N planets by SNR
@app.route('/get_top_planets', methods=['POST'])
def get_top_planets():
    data = request.json
    SNR0 = data.get('SNR0', 100)
    D = data.get('D', 6)
    SNR_filter = data.get('SNR_filter', 5)
    top_n = data.get('top_n', 10)

    # Create a temporary DataFrame to calculate SNR with user's parameters
    temp_df = global_df.copy()
    temp_df = calculate_snr(temp_df, SNR0, D)

    # Get the top 'top_n' records based on the SNR column
    top_records = temp_df.nlargest(top_n, 'snr')

    # Select and rename the columns to match the desired JSON format
    top_records = top_records[['pl_name', 'hostname', 'sy_snum', 'disc_year', 'pl_rade', 'st_rad', 'st_teff', 'sy_dist']]
    
    # Get the count of rows where snr > SNR_filter
    SNR_filter_count = temp_df[temp_df['snr'] > SNR_filter].shape[0]

    result = {
        "SNR_filter_count": SNR_filter_count,
        "top_planets": top_records.to_dict(orient='records')
    }
    
    return jsonify(result)

# Route to get K nearest neighbors for a given planet name
@app.route('/get_nearest_neighbors', methods=['POST'])
def get_nearest_neighbors():
    data = request.json
    SNR0 = data.get('SNR0', 100)
    D = data.get('D', 6)
    pl_name = data.get('pl_name', '7 CMa b')
    k = data.get('k', 5)

    # Create a temporary DataFrame to calculate SNR with user's parameters
    temp_df = global_df.copy()
    temp_df = calculate_snr(temp_df, SNR0, D)

    # Select features for the KNN search
    features = ['X', 'Y', 'Z', 'st_rad', 'st_teff', 'pl_orbsmax', 'habitability_score']
    feature_data = temp_df[features]

    feature_data = feature_data.apply(lambda x: x.fillna(x.median()), axis=0)
    
    scaler = StandardScaler()
    feature_data_scaled = scaler.fit_transform(feature_data)
    

    # Check if the given planet name exists
    if pl_name not in temp_df['pl_name'].values:
        return jsonify({"error": "Planet name not found"}), 404

    # Get the index of the planet with the given name
    target_index = temp_df[temp_df['pl_name'] == pl_name].index[0]
    target_features = feature_data_scaled[target_index].reshape(1, -1)

    # Perform KNN to find the nearest neighbors
    knn = NearestNeighbors(n_neighbors=k + 1)  # Include the target planet itself
    knn.fit(feature_data_scaled)
    distances, indices = knn.kneighbors(target_features)

    # Exclude the target planet itself from the results
    neighbor_indices = indices[0][1:]
    nearest_neighbors = temp_df.iloc[neighbor_indices]

    # Select the columns for the output
    nearest_neighbors = nearest_neighbors[['pl_name', 'hostname', 'sy_snum', 'disc_year', 'pl_rade', 'st_rad', 'st_teff', 'sy_dist']]

    result = nearest_neighbors.to_dict(orient='records')
    return jsonify(result)

# Route to get the full row for a given planet name
@app.route('/get_planet_details', methods=['POST'])
def get_planet_details():
    data = request.json
    pl_name = data.get('pl_name', '7 CMa b')

    # Check if the given planet name exists
    if pl_name not in global_df['pl_name'].values:
        return jsonify({"error": "Planet name not found"}), 404

    # Get the full row for the specified planet
    planet_details = global_df[global_df['pl_name'] == pl_name].to_dict(orient='records')[0]

    result = {
        "planet_details": planet_details
    }

    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
