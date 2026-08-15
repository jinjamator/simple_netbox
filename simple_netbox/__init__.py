from .rest_client.api import API
from .rest_client.resource import Resource
from .rest_client.request import make_request
from .rest_client.models import Request
from .rest_client.exceptions import NotFoundError
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


# --- scope ------------------------------------------------------------------
# A client can be bound to a scope: implicit filters applied to every query, so a
# caller that pins tenant=acme once never sees another tenant's objects. NetBox
# rejects a filter an endpoint does not know (HTTP 400), so scope is applied
# through an explicit per-endpoint table rather than blindly: the key is the
# canonical scope name, the value the query parameter that expresses it there.
SCOPE_KEYS = ("site", "tenant", "role", "platform", "tag", "status")

ENDPOINT_SCOPE_FILTERS = {
    "dcim/devices": {
        "site": "site",
        "tenant": "tenant",
        "role": "role",
        "platform": "platform",
        "tag": "tag",
        "status": "status",
    },
    "dcim/racks": {"site": "site", "tenant": "tenant", "tag": "tag", "status": "status"},
    # On the site list itself, the scope's site selects that very site.
    "dcim/sites": {"site": "slug", "tenant": "tenant", "tag": "tag", "status": "status"},
    "dcim/interfaces": {"site": "site", "tag": "tag"},
    "dcim/device-types": {"tag": "tag"},
    "ipam/ip-addresses": {"tenant": "tenant", "tag": "tag", "status": "status"},
    "ipam/prefixes": {
        "site": "site",
        "tenant": "tenant",
        "tag": "tag",
        "status": "status",
    },
    "ipam/vlans": {"site": "site", "tenant": "tenant", "tag": "tag", "status": "status"},
    "virtualization/virtual-machines": {
        "site": "site",
        "tenant": "tenant",
        "role": "role",
        "platform": "platform",
        "tag": "tag",
        "status": "status",
    },
    # Likewise, on the tenant list the scope's tenant selects that tenant.
    "tenancy/tenants": {"tenant": "slug", "tag": "tag"},
    # Organisational endpoints carry no scopeable dimension of their own: listing
    # every platform or role is not a tenancy leak, and filtering them by site
    # would silently return nothing.
    "dcim/platforms": {},
    "dcim/device-roles": {},
    "extras/tags": {},
}

# Friendly names used by the convenience accessors below.
ENDPOINTS = {
    "devices": "dcim/devices",
    "device_roles": "dcim/device-roles",
    "device_types": "dcim/device-types",
    "interfaces": "dcim/interfaces",
    "ip_addresses": "ipam/ip-addresses",
    "platforms": "dcim/platforms",
    "prefixes": "ipam/prefixes",
    "racks": "dcim/racks",
    "sites": "dcim/sites",
    "tags": "extras/tags",
    "tenants": "tenancy/tenants",
    "virtual_machines": "virtualization/virtual-machines",
    "vlans": "ipam/vlans",
}

DEFAULT_PAGE_SIZE = 200


def filter_value(value):
    """Normalise a filter value to what the NetBox query API expects.

    NetBox filters match on slugs (``?site=vienna``), so an object handed back by
    a previous query can be passed straight through as a filter value: a dict is
    reduced to its ``slug``, else its ``name``, else its ``id``. Lists are
    normalised element-wise and become repeated query parameters (NetBox ORs
    them), which is what makes ``tag=["core", "edge"]`` work.
    """
    if isinstance(value, (list, tuple, set)):
        return [filter_value(item) for item in value]
    if isinstance(value, dict):
        for key in ("slug", "name", "id"):
            if value.get(key) is not None:
                return value[key]
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    # str subclasses (jinjamator's SecretStr, for one) must not travel into a URL
    # as themselves.
    if isinstance(value, str):
        return str(value)
    return value


def object_id(obj):
    """Return the NetBox id of ``obj``, which may be an object dict or an id."""
    if isinstance(obj, dict):
        return obj.get("id")
    if isinstance(obj, bool):
        return None
    if isinstance(obj, int):
        return obj
    if isinstance(obj, str) and obj.isdigit():
        return int(obj)
    return None


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
        self._scope = {
            k: filter_value(v)
            for k, v in (kwargs.get("scope") or {}).items()
            if v not in (None, "")
        }
        self._page_size = int(kwargs.get("page_size") or DEFAULT_PAGE_SIZE)
        self._resources_by_path = {}

        self.api = API(
            api_root_url=self._base_url,  # base api url
            params={"format": "json"},  # default params
            headers={
                "Accept": "application/json;",
                "User-Agent": "simple_netbox",
                "authorization": build_auth_header(self._token, self._key),
            },  # default headers
            timeout=kwargs.get("timeout", 10),  # default timeout in seconds
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

    # --- scope ---------------------------------------------------------------

    @property
    def scope(self):
        """The implicit filters applied to every scopeable query."""
        return dict(self._scope)

    def set_scope(self, **scope):
        """Replace the client's scope. Keys are :data:`SCOPE_KEYS`."""
        self._scope = {
            k: filter_value(v) for k, v in scope.items() if v not in (None, "")
        }
        return self.scope

    def _scoped_params(self, path, params, scope=True):
        """Merge the scope into ``params`` for ``path``.

        ``scope`` may be ``True`` (the client's scope), ``False``/``None`` (none
        at all — the cross-tenant escape hatch) or a dict overriding it for this
        call. An explicit filter always wins over the scope: passing
        ``tenant="other"`` means that tenant, scope or no scope.
        """
        params = {k: filter_value(v) for k, v in (params or {}).items()}
        if scope is False or scope is None:
            return params
        active = self._scope if scope is True else {
            k: filter_value(v) for k, v in scope.items() if v not in (None, "")
        }
        if not active:
            return params
        applicable = ENDPOINT_SCOPE_FILTERS.get(self.normalize_path(path))
        if applicable is None:
            # An endpoint we have no table entry for: applying a guessed filter
            # would either 400 or, worse, silently return the wrong set.
            return params
        for key, value in active.items():
            param = applicable.get(key)
            if param and param not in params:
                params[param] = value
        return params

    # --- requests ------------------------------------------------------------

    @staticmethod
    def normalize_path(path):
        """``/dcim/devices/`` -> ``dcim/devices``."""
        return str(path).strip("/")

    def resource(self, path):
        """Return the :class:`NetboxResource` for an API path such as
        ``dcim/devices``.

        Built from the path directly rather than by walking ``api.dcim.devices``:
        attribute access cannot express a hyphen (``dcim/device-types``), and a
        path segment that happens to share a name with a resource attribute
        (``params``, ``get``, ``patch``) would otherwise resolve to that
        attribute instead of to an endpoint. Cached per path, because each
        resource owns an ``httpx.Client``.
        """
        path = self.normalize_path(path)
        resource = self._resources_by_path.get(path)
        if resource is None:
            resource = self.api._resource_class(
                api_root_url=self.api.api_root_url,
                resource_name=path,
                params=self.api.params,
                headers=self.api.headers,
                timeout=self.api.timeout,
                append_slash=self.api.append_slash,
                json_encode_body=self.api.json_encode_body,
                ssl_verify=self.api.ssl_verify,
                ep_suffix=self.api._ep_suffix,
                curl_commands=self.api.curl_commands,
                log_curl=self.api._log_curl,
                **self.api._kwargs,
            )
            self._resources_by_path[path] = resource
        return resource

    def iter_objects(
        self, path, params=None, scope=True, page_size=None, max_results=None
    ):
        """Yield every object of a list endpoint, following NetBox's pagination.

        :param path: API path, e.g. ``dcim/devices``.
        :param params: Query parameters; values are normalised by
            :func:`filter_value`.
        :param scope: ``True`` for the client scope, ``False`` for none, or a
            dict of scope overrides.
        :param page_size: Objects per request (default 200 — NetBox caps this at
            its ``MAX_PAGE_SIZE``, which the loop handles by following ``next``).
        :param max_results: Stop after this many objects.
        :return: A generator of object dicts.
        """
        params = self._scoped_params(path, params, scope)
        resource = self.resource(path)
        limit = int(page_size or self._page_size)
        offset = 0
        yielded = 0
        while True:
            page_params = dict(params)
            page_params["limit"] = limit
            if offset:
                page_params["offset"] = offset
            body = resource.get(params=page_params)
            if not isinstance(body, dict) or "results" not in body:
                # A detail or non-paginated endpoint reached through the generic
                # path: hand it back once rather than pretending it is a page.
                if body is not None:
                    yield body
                return
            results = body.get("results") or []
            for obj in results:
                yield obj
                yielded += 1
                if max_results and yielded >= max_results:
                    return
            # An empty page with a "next" would loop forever; NetBox does not do
            # that, but a proxy returning a truncated body could.
            if not body.get("next") or not results:
                return
            offset += len(results)

    def objects(self, path, params=None, scope=True, **kwargs):
        """:func:`iter_objects` as a list."""
        return list(self.iter_objects(path, params=params, scope=scope, **kwargs))

    def first(self, path, params=None, scope=True, **kwargs):
        """The first object of a list endpoint, or ``None``.

        Asks NetBox for a single row rather than a full page and discarding it,
        which is what makes ``device(name=…)`` a cheap lookup.
        """
        kwargs.setdefault("page_size", 1)
        for obj in self.iter_objects(
            path, params=params, scope=scope, max_results=1, **kwargs
        ):
            return obj
        return None

    def count(self, path, params=None, scope=True):
        """The number of objects matching ``params``, without fetching them."""
        body = self.resource(path).get(
            params=dict(self._scoped_params(path, params, scope), limit=1)
        )
        return (body or {}).get("count", 0)

    def get(self, path, scope=True, **params):
        """Escape hatch: query any endpoint, scope-aware.

        List endpoints come back as a fully paginated list of objects; anything
        else (``status``, a detail path like ``dcim/devices/12``) as the raw
        parsed body.

        :param path: API path, e.g. ``ipam/vrfs`` or ``dcim/devices/12``.
        :param scope: ``True``/``False``/dict, as in :func:`iter_objects`.
        :param params: Query parameters.
        """
        body = self.resource(path).get(
            params=self._scoped_params(path, params, scope)
        )
        if isinstance(body, dict) and "results" in body and "count" in body:
            if body.get("next"):
                return self.objects(path, params=params, scope=scope)
            return body["results"]
        return body

    def retrieve(self, path, id, **params):
        """Fetch one object by id, or ``None`` if it does not exist."""
        try:
            return self.resource(path).retrieve(id, params=params)
        except NotFoundError:
            return None

    def status(self):
        """NetBox's ``/api/status/`` — the cheapest proof that the URL is a
        NetBox and the token is accepted. Raises on both counts, so it is what a
        caller should use to fail fast at start rather than at object 40."""
        return self.resource("status").get()

    # --- objects -------------------------------------------------------------

    def devices(self, scope=True, **filters):
        """List devices (``dcim/devices``), scope applied, fully paginated."""
        return self.objects(ENDPOINTS["devices"], params=filters, scope=scope)

    def device(self, name=None, id=None, ip=None, scope=True, **filters):
        """Resolve exactly one device.

        :param name: Device name — matched exactly, as NetBox's ``name`` filter
            does.
        :param id: NetBox id; the cheapest lookup, and the only one unaffected by
            scope.
        :param ip: An address on the device (with or without mask). Resolved
            through ``ipam/ip-addresses`` to its assigned interface's device,
            which is how a device is found by management IP.
        :param filters: Any further NetBox device filter.
        :return: The device dict, or ``None``.
        """
        if id is not None:
            return self.retrieve(ENDPOINTS["devices"], object_id(id))
        if ip is not None:
            return self._device_by_ip(ip, scope=scope)
        if name is not None:
            filters["name"] = name
        if not filters:
            return None
        return self.first(ENDPOINTS["devices"], params=filters, scope=scope)

    def _device_by_ip(self, ip, scope=True):
        ip = str(ip).strip()
        candidates = self.objects(
            ENDPOINTS["ip_addresses"], params={"address": ip}, scope=scope
        )
        if not candidates:
            # ``address`` wants the address as NetBox stores it; ``q`` is the
            # substring search that still finds 10.0.0.1 stored as 10.0.0.1/24
            # on a NetBox that declines the bare form.
            candidates = self.objects(
                ENDPOINTS["ip_addresses"], params={"q": ip}, scope=scope
            )
        for address in candidates:
            assigned = address.get("assigned_object") or {}
            device = assigned.get("device") or {}
            device_id = object_id(device)
            if device_id:
                return self.retrieve(ENDPOINTS["devices"], device_id)
        return None

    def sites(self, scope=True, **filters):
        """List sites (``dcim/sites``)."""
        return self.objects(ENDPOINTS["sites"], params=filters, scope=scope)

    def tenants(self, scope=True, **filters):
        """List tenants (``tenancy/tenants``)."""
        return self.objects(ENDPOINTS["tenants"], params=filters, scope=scope)

    def tags(self, scope=True, **filters):
        """List tags (``extras/tags``)."""
        return self.objects(ENDPOINTS["tags"], params=filters, scope=scope)

    def platforms(self, scope=True, **filters):
        """List platforms (``dcim/platforms``)."""
        return self.objects(ENDPOINTS["platforms"], params=filters, scope=scope)

    def device_roles(self, scope=True, **filters):
        """List device roles (``dcim/device-roles``)."""
        return self.objects(ENDPOINTS["device_roles"], params=filters, scope=scope)

    def racks(self, scope=True, **filters):
        """List racks (``dcim/racks``)."""
        return self.objects(ENDPOINTS["racks"], params=filters, scope=scope)

    def interfaces(self, device=None, scope=True, **filters):
        """List interfaces (``dcim/interfaces``), optionally of one device.

        :param device: A device dict, id, or name.
        """
        if device is not None:
            device_id = object_id(device)
            if device_id is not None:
                filters["device_id"] = device_id
            else:
                filters["device"] = filter_value(device)
        return self.objects(ENDPOINTS["interfaces"], params=filters, scope=scope)

    def ip_addresses(self, scope=True, **filters):
        """List IP addresses (``ipam/ip-addresses``)."""
        return self.objects(ENDPOINTS["ip_addresses"], params=filters, scope=scope)

    # --- writes --------------------------------------------------------------

    def set_device_field(self, device, **fields):
        """Patch fields on a device — the write half of the surface.

        Custom fields are set by nesting them: ``set_device_field(dev,
        custom_fields={"target_os_version": "17.9.4"})`` merges into what NetBox
        already holds, so unrelated custom fields are preserved.

        :param device: A device dict, id, or name.
        :param fields: Fields to patch.
        :return: The updated device dict.
        """
        device_id = object_id(device)
        if device_id is None:
            resolved = self.device(name=filter_value(device))
            device_id = object_id(resolved)
        if device_id is None:
            raise ValueError(f"cannot resolve device {device!r}")
        # auto_slug would add a slug to the body because devices have a name —
        # and a device has no slug field, so NetBox would reject the patch.
        return self.resource(ENDPOINTS["devices"]).patch(
            device_id, body=fields, auto_slug=False
        )
