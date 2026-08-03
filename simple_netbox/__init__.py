from .rest_client.api import API
from .rest_client.resource import Resource
from .rest_client.request import make_request
from .rest_client.models import Request
from pprint import pformat
from types import MethodType
import base64
import os
import logging
import time
import re


# NetBox 4.6 introduced v2 API tokens, which authenticate with
#   Authorization: Bearer nbt_<key>.<token>
# instead of v1's
#   Authorization: Token <token>
# The prefix is NetBox's users.constants.TOKEN_PREFIX; kept here as a constant so
# a future change upstream is a one-line edit.
TOKEN_PREFIX_V2 = "nbt_"


def build_auth_header(token, key=None):
    """Return the Authorization header value for a NetBox API token.

    ``key`` is the v2 token's public key half. Supplying it selects v2; leaving it
    out keeps the v1 form, so existing callers are unaffected.
    """
    if key:
        return f"Bearer {TOKEN_PREFIX_V2}{key}.{token}"
    return f"Token {token}"


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

        # A base url without a trailing slash used to produce ".../netboxapi/":
        # the old test sliced the wrong end of the string and threw its own result
        # away, so the separator was never appended.
        if not self._base_url.endswith("/"):
            self._base_url += "/"
        self._base_url = self._base_url + "api/"
        self._token = kwargs.get("token", None)
        # v2 tokens only; see build_auth_header.
        self._key = kwargs.get("key", None)

        self.api = API(
            api_root_url=self._base_url,  # base api url
            params={"format": "json"},  # default params
            headers={
                "Accept": "application/json;",
                "User-Agent": "simple_netbox",
                "authorization": build_auth_header(self._token, self._key),
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

    def login(self, token, key=None):
        """Replace the credential on an existing client.

        The header is rewritten in place because resources share this very dict,
        so already-created ones pick the new credential up too. (The key used to
        be misspelled "authorizationn", which meant this method silently never
        authenticated anything.)
        """
        if token:
            self._token = token
        if key:
            self._key = key
        self.api.headers["authorization"] = build_auth_header(self._token, self._key)
        return True
