import streamlit as st
from src.data_processing.tmdb_client import TMDBWrapper
import pandas as pd
import zipfile
import io

def validate_file(file):
    if file.name.endswith('.csv'):
        return pd.read_csv(file)
    elif file.name.endswith('.xls') or file.name.endswith('.xlsx'):
        return pd.read_excel(file)
    else:
        st.error("Unsupported file format. Please upload a CSV or XLS file.")
        return None

def process_zip(file):
    with zipfile.ZipFile(file) as z:
        dfs = []
        for filename in z.namelist():
            if filename.endswith('.xls') or filename.endswith('.xlsx'):
                with z.open(filename) as f:
                    dfs.append(pd.read_excel(f))
        return pd.concat(dfs, ignore_index=True)

def display_movie_card(title: str):
    """
    Display a movie card with details fetched from TMDB.
    
    Parameters:
    title (str): The title of the movie to display.
    """
    client = TMDBWrapper()
    details = client.get_movie_details(title)
    
    if details:
        with st.container():
            col_img, col_data = st.columns([1, 2])
            
            with col_img:
                st.image(
                    f"https://image.tmdb.org/t/p/w500{details['poster_path']}",
                    width=300
                )
                
            with col_data:
                st.markdown(f"### {details['title']} ({details['release_date'][:4]})")
                st.caption(f"Duration: {details['runtime']} minutes")
                st.write(f"**Genres:** {', '.join([g['name'] for g in details['genres']])}")
                st.write(f"**Director:** {client.get_director(details['id'])}")
                st.progress(details['vote_average'] / 10)
                
    else:
        st.error("Movie not found on TMDB")

# Main flow usage
st.title("Letterboxd Analytics")

uploaded_file = st.file_uploader("Upload your Letterboxd export (ZIP)", type=["zip"])

if uploaded_file:
    df = process_zip(uploaded_file)
    if df is not None:
        st.success("File uploaded and validated successfully!")
        st.write(df.head())
        selected_movie = st.selectbox("Choose a movie", df['Title'].unique())
        display_movie_card(selected_movie)