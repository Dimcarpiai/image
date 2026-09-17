# Mirrors the deployment recipe from the framework README.
workers = 1  # jobs run in-process; keep one worker so a job and its polling share state
keepalive = 30
worker_class = "uvicorn.workers.UvicornH11Worker"
bind = ["0.0.0.0:8080"]
accesslog = "-"
errorlog = "-"
loglevel = "info"
# Needed so url_for() builds the public https URL used in the manifest.
# Restrict to your proxy's IP in production.
forwarded_allow_ips = "*"
