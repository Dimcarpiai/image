import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.sqlite3")
os.environ["DEVELOPMENT_AUTH_TOKEN"] = "dev-token"
