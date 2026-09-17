import os
import tempfile

d = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(d, "test.sqlite3")
os.environ["DATA_DIR"] = d
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DEVELOPMENT_AUTH_TOKEN"] = ""
