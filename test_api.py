from dotenv import load_dotenv
import os
import requests

load_dotenv()

def test_tmdb_conection():
    api_key = os.getenv("TMDB_API_KEY")
    response = requests.get(f"https://api.themoviedb.org/3/movie/550?api_key={api_key}")
    
    if response.status_code == 200:
        print("API response:", response.status_code)
        print("Movie name:", response.json()["title"])

if __name__ == "__main__":
    test_tmdb_conection()
    