import tmdbsimple as tmdb
import os
from dotenv import load_dotenv
from typing import Optional, Dict

# Load environment variables from a .env file
load_dotenv()

class TMDBWrapper:
    def __init__(self):
        # Initialize the TMDB API key from environment variables
        tmdb.API_KEY = os.getenv('TMDB_API_KEY')
        self.language = 'es-MX'
    
    def get_movie_details(self, title: str, year: int = None) -> Optional[Dict]:
        """
        Retrieves movie details using advanced search.
        
        Parameters:
        title (str): The title of the movie.
        year (int, optional): The release year of the movie.
        
        Returns:
        Optional[Dict]: A dictionary containing movie details if found, otherwise None.
        """
        try:
            search = tmdb.Search()
            response = search.movie(
                query=title,
                year=year,
                language=self.language,
                include_adult=False
            )
            
            if search.results:
                movie_id = search.results[0]['id']
                movie = tmdb.Movies(movie_id)
                return movie.info(language=self.language)
            return None
            
        except Exception as e:
            print(f"TMDB Error: {str(e)}")
            return None

    def get_director(self, movie_id: int) -> str:
        """
        Retrieves the director(s) of a movie.
        
        Parameters:
        movie_id (int): The TMDB ID of the movie.
        
        Returns:
        str: A comma-separated string of director names.
        """
        credits = tmdb.Movies(movie_id).credits()
        directors = [
            member['name'] 
            for member in credits['crew'] 
            if member['job'] == 'Director'
        ]
        return ', '.join(directors)