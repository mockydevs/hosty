"""Spec dataclasses (v2/M1): construction IS validation — every
cross-reference rule, plus spec_hash inclusion/exclusion semantics."""

from __future__ import annotations

import pytest

from app.domain.specs import (
    EndpointSpec,
    ObservedStack,
    ObservedUnit,
    ServiceSpec,
    StackSpec,
    VolumeSpec,
    spec_hash,
)
from app.domain.validate import SpecValidationError


def make_service(**overrides) -> ServiceSpec:
    base = dict(name="web", image="nginx:1.27", internal_port=80, is_web=True)
    base.update(overrides)
    return ServiceSpec(**base)


def make_stack(**overrides) -> StackSpec:
    base = dict(name="blog", tenant="hosty-t-7", loopback_ip="127.1.0.1", services=(make_service(),))
    base.update(overrides)
    return StackSpec(**base)


# --- ServiceSpec -------------------------------------------------------------------


def test_service_env_must_be_sorted_canonical():
    ServiceSpec(name="web", image="nginx", env=(("A", "1"), ("B", "2")))
    with pytest.raises(SpecValidationError, match="sorted"):
        ServiceSpec(name="web", image="nginx", env=(("B", "2"), ("A", "1")))


def test_service_env_entries_validated():
    with pytest.raises(SpecValidationError):
        ServiceSpec(name="web", image="nginx", env=(("2BAD", "x"),))
    with pytest.raises(SpecValidationError):
        ServiceSpec(name="web", image="nginx", env=(("OK", "a\nb"),))


def test_exposed_requires_a_published_port():
    make_service(exposed=True)  # has host_port → fine
    with pytest.raises(SpecValidationError, match="publishes no port"):
        ServiceSpec(name="db", image="redis:7", exposed=True)  # no port → rejected



def test_service_port_values_validated():
    with pytest.raises(SpecValidationError):
        make_service(internal_port=0)


@pytest.mark.parametrize(("field", "bad"), [("memory_mb", 8), ("memory_mb", 2_000_000)])
def test_service_memory_bounds(field, bad):
    make_service(memory_mb=512)
    with pytest.raises(SpecValidationError, match="memory_mb"):
        make_service(**{field: bad})


@pytest.mark.parametrize("bad", [0, 6401])
def test_service_cpu_bounds(bad):
    make_service(cpu_percent=200)
    with pytest.raises(SpecValidationError, match="cpu_percent"):
        make_service(cpu_percent=bad)


def test_service_name_and_image_validated():
    with pytest.raises(SpecValidationError):
        make_service(name="-bad")
    with pytest.raises(SpecValidationError):
        make_service(image="-bad")


# --- VolumeSpec / EndpointSpec -----------------------------------------------------


def test_volume_spec_validates_all_fields():
    VolumeSpec(name="data", service="web", mount_path="/data")
    with pytest.raises(SpecValidationError):
        VolumeSpec(name="Data", service="web", mount_path="/data")
    with pytest.raises(SpecValidationError):
        VolumeSpec(name="data", service="w eb", mount_path="/data")
    with pytest.raises(SpecValidationError):
        VolumeSpec(name="data", service="web", mount_path="data")


def test_endpoint_spec_validates_all_fields():
    EndpointSpec(domain="blog.example.com", service="web")
    with pytest.raises(SpecValidationError):
        EndpointSpec(domain="-bad.example.com", service="web")
    with pytest.raises(SpecValidationError):
        EndpointSpec(domain="blog.example.com", service="WEB")


# --- StackSpec ---------------------------------------------------------------------


def test_stack_happy_path_and_helpers():
    stack = make_stack(
        services=(make_service(), make_service(name="db", internal_port=None, host_port=None)),
        volumes=(VolumeSpec(name="data", service="db", mount_path="/var/lib/db"),),
        endpoints=(EndpointSpec(domain="blog.example.com", service="web"),),
    )
    assert stack.network == "hosty-blog"
    assert stack.unit_base("web") == "blog-web"
    assert [vol.name for vol in stack.volumes_for("db")] == ["data"]
    assert stack.volumes_for("web") == ()


@pytest.mark.parametrize("tenant", ["root", "hosty-t-", "hosty-t-x", "site-abc", 42, None])
def test_stack_rejects_bad_tenant(tenant):
    with pytest.raises(SpecValidationError, match="tenant"):
        make_stack(tenant=tenant)


def test_stack_rejects_bad_name():
    with pytest.raises(SpecValidationError):
        make_stack(name="Blog")


def test_stack_requires_services():
    with pytest.raises(SpecValidationError, match="no services"):
        make_stack(services=())


def test_stack_rejects_duplicate_service_names():
    with pytest.raises(SpecValidationError, match="duplicate service"):
        make_stack(services=(make_service(), make_service(host_port=20002)))


def test_stack_allows_shared_volume_names_with_distinct_service_mounts():
    volumes = (
        VolumeSpec(name="data", service="web", mount_path="/app/data"),
        VolumeSpec(name="data", service="worker", mount_path="/data"),
    )
    stack = make_stack(services=(make_service(), make_service(name="worker")), volumes=volumes)
    assert [vol.service for vol in stack.volumes_for("worker")] == ["worker"]


def test_stack_rejects_duplicate_volume_names_with_conflicting_mounts_on_same_service():
    volumes = (
        VolumeSpec(name="data", service="web", mount_path="/a"),
        VolumeSpec(name="data", service="web", mount_path="/b"),
    )
    with pytest.raises(SpecValidationError, match="duplicate volume"):
        make_stack(volumes=volumes)


def test_stack_rejects_volume_for_unknown_service():
    with pytest.raises(SpecValidationError, match="unknown service"):
        make_stack(volumes=(VolumeSpec(name="data", service="ghost", mount_path="/d"),))


def test_stack_rejects_duplicate_endpoint_domains():
    endpoints = (
        EndpointSpec(domain="a.example.com", service="web"),
        EndpointSpec(domain="a.example.com", service="web"),
    )
    with pytest.raises(SpecValidationError, match="duplicate endpoint"):
        make_stack(endpoints=endpoints)


def test_stack_rejects_endpoint_to_unknown_service():
    with pytest.raises(SpecValidationError, match="missing service"):
        make_stack(endpoints=(EndpointSpec(domain="a.example.com", service="ghost"),))


def test_stack_rejects_endpoint_to_portless_service():
    services = (make_service(internal_port=None),)
    endpoints = (EndpointSpec(domain="a.example.com", service="web"),)
    with pytest.raises(SpecValidationError, match="no internal_port"):
        make_stack(services=services, endpoints=endpoints)


# --- spec_hash ---------------------------------------------------------------------


def test_spec_hash_is_stable_and_short():
    stack = make_stack()
    assert spec_hash(stack, stack.services[0]) == spec_hash(stack, stack.services[0])
    assert len(spec_hash(stack, stack.services[0])) == 32


@pytest.mark.parametrize(
    "change",
    [
        {"image": "nginx:1.28"},
        {"env": (("K", "v"),)},
        {"internal_port": 81},
        {"memory_mb": 256},
        {"cpu_percent": 50},
    ],
)
def test_spec_hash_tracks_runtime_shape(change):
    base = make_stack()
    changed = make_stack(services=(make_service(**change),))
    assert spec_hash(base, base.services[0]) != spec_hash(changed, changed.services[0])


def test_spec_hash_tracks_volumes_and_network_but_not_endpoints():
    base = make_stack()
    with_volume = make_stack(volumes=(VolumeSpec(name="d", service="web", mount_path="/d"),))
    assert spec_hash(base, base.services[0]) != spec_hash(with_volume, with_volume.services[0])

    renamed = make_stack(name="shop")  # network name derives from stack name
    assert spec_hash(base, base.services[0]) != spec_hash(renamed, renamed.services[0])

    routed = make_stack(endpoints=(EndpointSpec(domain="a.example.com", service="web"),))
    assert spec_hash(base, base.services[0]) == spec_hash(routed, routed.services[0])


# --- observed-state carriers -------------------------------------------------------


def test_observed_dataclasses_compare_by_value():
    a = ObservedStack(tenant="hosty-t-1", units={"web": ObservedUnit("h", active=True)})
    b = ObservedStack(tenant="hosty-t-1", units={"web": ObservedUnit("h", active=True)})
    assert a == b
    assert a.volume_dirs == frozenset()
    assert ObservedUnit(None, active=False) != ObservedUnit("h", active=False)
