from simple_netbox import NetboxClient
from yaml import safe_load
import logging

logging.basicConfig()
logging.getLogger().setLevel(logging.INFO)

URL = input("Please Enter Netbox URL: ") or "http://localhost:8000"
token = input("Please Enter the Netbox token: ") or "not set"


nb = NetboxClient(URL, token=token, log_curl_commands=True)

dcim = safe_load(open("demo.yaml"))["dcim"]


for k, v in dcim.items():
    for data in v:
        logging.info(f"ensuring dcim.{k}.{data.get('name')} exists and up-to-date")
        nb.api.dcim(k).ensure_exists(**data)
