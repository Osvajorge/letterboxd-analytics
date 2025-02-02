import streamlit as st
from src.data_processing.tmdb_client import TMDBWrapper
import pandas as pd
import zipfile
import io

# Page configuration
st.set_page_config(
    page_title="Letterboxd Analytics",
    page_icon="🎬",
    layout="wide"
)

# Custom styles
st.markdown("""
    <style>
    .main {
        padding: 2rem;
    }
    .stProgress > div > div > div > div {
        background-color: #F5C518;
    }
    </style>
""", unsafe_allow_html=True)

# Validation and processing functions
@st.cache_data
def validate_file(file):
    try:
        if file.name.endswith('.csv'):
            return pd.read_csv(file)
        elif file.name.endswith(('.xls', '.xlsx')):
            return pd.read_excel(file)
        else:
            st.error("Unsupported format. Please upload a CSV or Excel file.")
            return None
    except Exception as e:
        st.error(f"Error processing file: {str(e)}")
        return None

@st.cache_data
def process_zip(file):
    try:
        with zipfile.ZipFile(file) as z:
            dfs = []
            for filename in z.namelist():
                if filename.endswith(('.xls', '.xlsx', '.csv')):
                    with z.open(filename) as f:
                        if filename.endswith('.csv'):
                            dfs.append(pd.read_csv(f))
                        else:
                            dfs.append(pd.read_excel(f))
            if not dfs:
                st.error("No valid files found in ZIP")
                return None
            return pd.concat(dfs, ignore_index=True)
    except Exception as e:
        st.error(f"Error processing ZIP file: {str(e)}")
        return None

def display_movie_card(title: str):
    with st.spinner('Loading movie information...'):
        client = TMDBWrapper()
        details = client.get_movie_details(title)
        
        if details:
            with st.container():
                col_img, col_data = st.columns([1, 2])
                
                with col_img:
                    if details.get('poster_path'):
                        st.image(
                            f"https://image.tmdb.org/t/p/w500{details['poster_path']}",
                            width=300
                        )
                    else:
                        st.image("https://via.placeholder.com/300x450?text=No+Poster", width=300)
                    
                with col_data:
                    st.markdown(f"### {details['title']} ({details['release_date'][:4]})")
                    st.caption(f"⏱️ {details.get('runtime', 'N/A')} minutes")
                    if details.get('genres'):
                        st.write(f"🎭 **Genres:** {', '.join([g['name'] for g in details['genres']])}")
                    director = client.get_director(details['id'])
                    if director:
                        st.write(f"🎬 **Director:** {director}")
                    st.write(f"⭐ **TMDB Rating:** {details.get('vote_average', 'N/A')}/10")
                    st.progress(details.get('vote_average', 0) / 10)
                    
                    if details.get('overview'):
                        st.markdown("### Overview")
                        st.write(details['overview'])
        else:
            st.warning("Movie not found in TMDB")

# Main interface
st.title("🎬 Letterboxd Analytics")
st.write("Analyze your Letterboxd viewing history")

uploaded_file = st.file_uploader(
    "Upload your Letterboxd export (ZIP)",
    type=["zip"],
    help="Export your data from Letterboxd → Settings → Import & Export → Export Your Data"
)

if uploaded_file:
    with st.spinner('Processing file...'):
        df = process_zip(uploaded_file)
        if df is not None:
            st.success("File processed successfully!")
            
            # Basic metrics
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Movies", len(df))
            with col2:
                st.metric("Unique Movies", df['Title'].nunique())
            with col3:
                st.metric("Average Rating", f"{df['Rating'].mean():.1f}⭐")
            
            # Movie selector
            selected_movie = st.selectbox(
                "Select a movie",
                df['Title'].unique(),
                index=None,
                placeholder="Choose a movie to see details..."
            )
            
            if selected_movie:
                display_movie_card(selected_movie)