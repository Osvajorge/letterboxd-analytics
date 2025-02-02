import streamlit as st
from src.data_processing.tmdb_client import TMDBWrapper
import pandas as pd
import zipfile
import io

# Configuración de la página
st.set_page_config(
    page_title="Letterboxd Analytics",
    page_icon="🎬",
    layout="wide"
)

# Estilos personalizados
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

# Funciones de validación y procesamiento
@st.cache_data
def validate_file(file):
    try:
        if file.name.endswith('.csv'):
            return pd.read_csv(file)
        elif file.name.endswith(('.xls', '.xlsx')):
            return pd.read_excel(file)
        else:
            st.error("Formato no soportado. Por favor, sube un archivo CSV o Excel.")
            return None
    except Exception as e:
        st.error(f"Error al procesar el archivo: {str(e)}")
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
                st.error("No se encontraron archivos válidos en el ZIP")
                return None
            return pd.concat(dfs, ignore_index=True)
    except Exception as e:
        st.error(f"Error al procesar el archivo ZIP: {str(e)}")
        return None

def display_movie_card(title: str):
    with st.spinner('Cargando información de la película...'):
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
                    st.caption(f"⏱️ {details.get('runtime', 'N/A')} minutos")
                    if details.get('genres'):
                        st.write(f"🎭 **Géneros:** {', '.join([g['name'] for g in details['genres']])}")
                    director = client.get_director(details['id'])
                    if director:
                        st.write(f"🎬 **Director:** {director}")
                    st.write(f"⭐ **TMDB Rating:** {details.get('vote_average', 'N/A')}/10")
                    st.progress(details.get('vote_average', 0) / 10)
                    
                    if details.get('overview'):
                        st.markdown("### Sinopsis")
                        st.write(details['overview'])
        else:
            st.warning("Película no encontrada en TMDB")

# Interfaz principal
st.title("🎬 Letterboxd Analytics")
st.write("Analiza tu historial de películas vistas en Letterboxd")

uploaded_file = st.file_uploader(
    "Sube tu exportación de Letterboxd (ZIP)",
    type=["zip"],
    help="Exporta tus datos desde Letterboxd → Settings → Import & Export → Export Your Data"
)

if uploaded_file:
    with st.spinner('Procesando archivo...'):
        df = process_zip(uploaded_file)
        if df is not None:
            st.success("¡Archivo procesado exitosamente!")
            
            # Métricas básicas
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total de películas", len(df))
            with col2:
                st.metric("Películas únicas", df['Title'].nunique())
            with col3:
                st.metric("Calificación promedio", f"{df['Rating'].mean():.1f}⭐")
            
            # Selector de película
            selected_movie = st.selectbox(
                "Selecciona una película",
                df['Title'].unique(),
                index=None,
                placeholder="Escoge una película para ver detalles..."
            )
            
            if selected_movie:
                display_movie_card(selected_movie)