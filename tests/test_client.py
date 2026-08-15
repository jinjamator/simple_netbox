#!/usr/bin/env python3
"""Offline tests for the NetboxClient query layer.

No NetBox and no network: every resource's httpx.Client is replaced by a stub
that serves canned pages and records the requests, so pagination, scope
application and device resolution are asserted on the requests actually sent.

Run: python3 tests/test_client.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simple_netbox import (  # noqa: E402
    NetboxClient,
    build_auth_header,
    filter_value,
    object_id,
)


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.url = "http://netbox.invalid/api/"
        self.text = "{}"

    def json(self):
        return self._body


class FakeClient:
    """Stands in for httpx.Client on every resource of one NetboxClient."""

    def __init__(self, routes, log):
        self.routes = routes
        self.log = log

    def _handle(self, method, url, **options):
        params = options.get("params", {})
        self.log.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "body": options.get("json"),
            }
        )
        path = url.split("/api/", 1)[1].strip("/")
        route = self.routes.get((method, path)) or self.routes.get(path)
        if route is None:
            raise AssertionError(f"unexpected request {method} {path} {params}")
        body = route(params) if callable(route) else route
        return FakeResponse(body)

    def get(self, url, **options):
        return self._handle("GET", url, **options)

    def patch(self, url, **options):
        return self._handle("PATCH", url, **options)

    def post(self, url, **options):
        return self._handle("POST", url, **options)

    def close(self):
        pass


def make_client(routes, **kwargs):
    """A NetboxClient whose resources all talk to the stub."""
    log = []
    client = NetboxClient("https://netbox.invalid", token="t", **kwargs)
    real_resource = client.resource

    def stubbed_resource(path):
        resource = real_resource(path)
        if not isinstance(resource.client, FakeClient):
            resource.client = FakeClient(routes, log)
        return resource

    client.resource = stubbed_resource
    return client, log


def page(results, next_url=None, count=None):
    return {
        "count": count if count is not None else len(results),
        "next": next_url,
        "previous": None,
        "results": results,
    }


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok: {message}")


def test_auth_header():
    check(build_auth_header("abc") == "Token abc", "v1 token header")
    check(
        build_auth_header("abc", "key") == "Bearer nbt_key.abc",
        "v2 token header",
    )


def test_normalisation():
    check(filter_value({"slug": "vienna", "id": 3}) == "vienna", "dict -> slug")
    check(filter_value({"name": "Core", "id": 3}) == "Core", "dict -> name")
    check(filter_value(["a", {"slug": "b"}]) == ["a", "b"], "list normalised")
    check(filter_value(True) == "true", "bool -> netbox true")
    check(object_id({"id": 7}) == 7 and object_id("7") == 7, "object_id forms")
    check(object_id("core-sw-01") is None, "a name is not an id")


def test_pagination():
    def devices(params):
        offset = int(params.get("offset", 0))
        if offset == 0:
            return page(
                [{"id": 1}, {"id": 2}],
                next_url="https://netbox.invalid/api/dcim/devices/?offset=2",
                count=3,
            )
        return page([{"id": 3}], count=3)

    client, log = make_client({("GET", "dcim/devices"): devices})
    result = client.devices()
    check([d["id"] for d in result] == [1, 2, 3], "all pages collected")
    check(len(log) == 2, "one request per page")
    check(log[1]["params"]["offset"] == 2, "offset advances by page length")


def test_max_results_stops_early():
    def devices(params):
        return page(
            [{"id": 1}, {"id": 2}],
            next_url="https://netbox.invalid/api/dcim/devices/?offset=2",
            count=99,
        )

    client, log = make_client({("GET", "dcim/devices"): devices})
    result = list(client.iter_objects("dcim/devices", max_results=1))
    check(len(result) == 1, "max_results honoured")
    check(len(log) == 1, "no page fetched past the limit")


def test_scope_applied_only_where_supported():
    routes = {
        ("GET", "dcim/devices"): lambda p: page([{"id": 1}]),
        ("GET", "dcim/platforms"): lambda p: page([{"id": 2}]),
        ("GET", "dcim/sites"): lambda p: page([{"id": 3}]),
    }
    client, log = make_client(
        routes, scope={"tenant": "acme", "status": "active", "site": "vienna"}
    )

    client.devices()
    check(log[-1]["params"]["tenant"] == "acme", "scope tenant on devices")
    check(log[-1]["params"]["site"] == "vienna", "scope site on devices")

    client.platforms()
    check(
        "tenant" not in log[-1]["params"] and "site" not in log[-1]["params"],
        "no scope on an endpoint that cannot express it",
    )

    client.sites()
    check(
        log[-1]["params"].get("slug") == "vienna" and "site" not in log[-1]["params"],
        "scope site becomes slug on the site list",
    )

    client.devices(tenant="other")
    check(log[-1]["params"]["tenant"] == "other", "explicit filter beats scope")

    client.devices(scope=False)
    check("tenant" not in log[-1]["params"], "scope=False disables it")

    client.devices(scope={"tenant": "third"})
    check(
        log[-1]["params"]["tenant"] == "third" and "site" not in log[-1]["params"],
        "a scope dict replaces the client scope",
    )


def test_scope_unknown_endpoint_is_left_alone():
    client, log = make_client(
        {("GET", "ipam/vrfs"): lambda p: page([{"id": 1}])}, scope={"tenant": "acme"}
    )
    client.get("ipam/vrfs")
    check(
        "tenant" not in log[-1]["params"],
        "an untabled endpoint gets no guessed filter",
    )


def test_device_by_ip():
    routes = {
        ("GET", "ipam/ip-addresses"): lambda p: page(
            [
                {"id": 9, "address": "10.0.0.1/24", "assigned_object": None},
                {
                    "id": 10,
                    "address": "10.0.0.1/24",
                    "assigned_object": {"device": {"id": 42}},
                },
            ]
        )
        if p.get("address") == "10.0.0.1"
        else page([]),
        ("GET", "dcim/devices/42"): {"id": 42, "name": "core-sw-01"},
    }
    client, log = make_client(routes)
    device = client.device(ip="10.0.0.1")
    check(device["name"] == "core-sw-01", "device resolved from an address")
    check(
        log[0]["params"]["address"] == "10.0.0.1",
        "looked up by address first",
    )


def test_device_by_ip_falls_back_to_q():
    calls = {"n": 0}

    def addresses(params):
        calls["n"] += 1
        if "q" in params:
            return page([{"id": 1, "assigned_object": {"device": {"id": 7}}}])
        return page([])

    routes = {
        ("GET", "ipam/ip-addresses"): addresses,
        ("GET", "dcim/devices/7"): {"id": 7, "name": "edge-01"},
    }
    client, log = make_client(routes)
    device = client.device(ip="10.0.0.9")
    check(device["name"] == "edge-01", "fallback query resolves the device")
    check(calls["n"] == 2, "fallback only after the exact lookup misses")


def test_device_by_name_and_id():
    routes = {
        ("GET", "dcim/devices"): lambda p: page(
            [{"id": 5, "name": p.get("name")}] if p.get("name") else []
        ),
        ("GET", "dcim/devices/5"): {"id": 5, "name": "by-id"},
    }
    client, log = make_client(routes)
    check(client.device(name="core-sw-01")["id"] == 5, "device by name")
    check(log[-1]["params"]["limit"] == 1, "name lookup fetches a single row")
    check(client.device(id=5)["name"] == "by-id", "device by id")
    check(client.device() is None, "no selector -> None")


def test_interfaces_selector():
    client, log = make_client(
        {("GET", "dcim/interfaces"): lambda p: page([{"id": 1}])}
    )
    client.interfaces(device={"id": 3, "name": "sw"})
    check(log[-1]["params"]["device_id"] == 3, "device dict -> device_id")
    client.interfaces(device="sw-01")
    check(log[-1]["params"]["device"] == "sw-01", "device name -> device")


def test_set_device_field():
    routes = {("PATCH", "dcim/devices/42"): {"id": 42, "status": "offline"}}
    client, log = make_client(routes)
    result = client.set_device_field({"id": 42, "name": "core-sw-01"}, status="offline")
    check(result["status"] == "offline", "patch returns the updated device")
    check(log[-1]["body"] == {"status": "offline"}, "only the given fields are sent")
    check(
        "slug" not in (log[-1]["body"] or {}),
        "no slug is invented for a device patch",
    )


def test_count_and_first():
    client, log = make_client(
        {("GET", "dcim/devices"): lambda p: page([{"id": 1}], count=17)}
    )
    check(client.count("dcim/devices") == 17, "count comes from the envelope")
    check(log[-1]["params"]["limit"] == 1, "count does not fetch the objects")
    check(client.first("dcim/devices")["id"] == 1, "first returns one object")


def test_get_escape_hatch():
    routes = {
        ("GET", "ipam/vrfs"): lambda p: page([{"id": 1}, {"id": 2}]),
        ("GET", "status"): {"netbox-version": "4.6.2"},
    }
    client, log = make_client(routes)
    check([o["id"] for o in client.get("ipam/vrfs")] == [1, 2], "list endpoint -> list")
    check(client.status()["netbox-version"] == "4.6.2", "status returns the raw body")


def test_hyphenated_and_colliding_paths():
    routes = {
        ("GET", "dcim/device-types"): lambda p: page([{"id": 1}]),
        ("GET", "extras/tags"): lambda p: page([{"id": 2}]),
    }
    client, log = make_client(routes)
    check(len(client.get("dcim/device-types")) == 1, "hyphenated path reachable")
    check(
        client.resource("dcim/device-types") is client.resource("dcim/device-types"),
        "resources are cached per path",
    )
    check(len(client.tags()) == 1, "tags endpoint reachable")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        print(f"{test.__name__}:")
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
