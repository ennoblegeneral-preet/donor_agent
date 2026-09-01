web: gunicorn app:app --workers 1 --threads 4 --timeout 600 --bind 0.0.0.0:$PORT --max-requests 500 --max-requests-jitter 50 --log-level info --worker-class gthread

