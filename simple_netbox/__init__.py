from .rest_client.api import API
from .rest_client.resource import Resource
from .rest_client.request import make_request
from .rest_client.models import Request
from types import MethodType
import base64
import os
import logging
import time
import re


class NetboxResource(Resource):
    def ensure_exists(self, **kwargs):
        filter = {}
        if "id" in kwargs:
            filter["id"] = kwargs["id"]
        else:
            for k, v in kwargs.items():
                if k in ["slug", "name", "model", "value"]:
                    filter[k] = v
        res = self.get(params=filter)
        if res["count"] > 1:
            raise ValueError(
                f"found multiple results, for filter {filter} cannot proceed."
            )
        elif res["count"] == 1:
            return self.patch(res["results"][0]["id"], body=kwargs)
        else:
            return self.post(body=kwargs)

    def ensure_absent(self, **kwargs):
        filter = {}
        if "id" in kwargs:
            filter["id"] = kwargs["id"]
        else:
            for k, v in kwargs.items():
                if k in ["slug", "name", "model", "value"]:
                    filter[k] = v
        res = self.get(params=filter)
        if res["count"] > 1:
            raise ValueError(
                f"found multiple results, for filter {filter} cannot proceed."
            )
        elif res["count"] == 1:
            return self.delete(res["results"][0]["id"])


class NetboxClient(object):
    def __init__(self, url, **kwargs):
        self._log = logging.getLogger()
        self._base_url = url

        if self._base_url[:-1] != "/":
            self._base_url + "/"
        self._base_url = self._base_url + "api/"
        self._token = kwargs.get("token", None)

        self.api = API(
            api_root_url=self._base_url,  # base api url
            params={"format": "json"},  # default params
            headers={
                "Accept": "application/json;",
                "User-Agent": "simple_netbox",
                "authorization": f"Token {self._token}",
            },  # default headers
            timeout=10,  # default timeout in seconds
            append_slash=True,  # append slash to final url
            json_encode_body=True,  # encode body as json
            ssl_verify=kwargs.get("ssl_verify", None),
            resource_class=NetboxResource,
            log_curl_commands=kwargs.get("log_curl_commands", False),
            auto_slug=kwargs.get("auto_slug", True),
        )

    def __str__(self):
        return pformat(self.api.get_resource_list())

    def login(self, token):
        if token:
            self._token = token
        self.api.headers["authorizationn"] = f"Token {self._token}"
        return True
