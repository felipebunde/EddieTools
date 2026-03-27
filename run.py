from waitress import serve
from app import app  # certifica-se de que o nome do seu arquivo principal é app.py

serve(app, host='0.0.0.0', port=5050, threads=100)
