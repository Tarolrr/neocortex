"""Run with an installed environment's Python -I to check packaged UI assets."""

import http.client
import threading
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from nc.config import Config
from nc.ui import make_server

assert files("nc").joinpath("static/style.css").read_text()
with TemporaryDirectory(dir=Path.cwd()) as home:
    server = make_server(Config.load(Path(home)), 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        client.request("GET", "/static/style.css")
        response = client.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/css"
        assert response.read()
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
print("Installed UI assets served successfully")
