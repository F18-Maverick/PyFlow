"""Receiver-scoped allocator occupancy must not overwrite a live upload."""

from PyFlow.network_api.connect_tcp import FID_LOW_BITS, _FidAllocator


def test_allocator_skips_a_live_upload_in_the_same_receiver_namespace():
    receiver = ("127.0.0.1", 40001)
    uploads = {}
    allocator = _FidAllocator((uploads, {}, {}))
    candidate = (1 << FID_LOW_BITS) | (allocator._epoch << 1)
    entry = [0, 1024, None, "/source.bin", receiver]
    uploads[(receiver, candidate)] = entry

    allocated = allocator.alloc(receiver=receiver)

    assert allocated != candidate
    assert (receiver, allocated) not in uploads
    assert uploads[(receiver, candidate)] is entry


def test_another_receiver_does_not_occupy_the_candidate_namespace():
    receiver, other = ("127.0.0.1", 40001), ("127.0.0.1", 40002)
    uploads = {}
    allocator = _FidAllocator((uploads, {}, {}))
    candidate = (1 << FID_LOW_BITS) | (allocator._epoch << 1)
    uploads[(other, candidate)] = [0, 1024, None, "/other.bin", other]

    assert allocator.alloc(receiver=receiver) == candidate
    assert (receiver, candidate) not in uploads

def test_allocator_skips_restored_route_primary_and_scoped_downstream_ids():
    receiver = ("127.0.0.1", 40001)
    other_receiver = ("127.0.0.1", 40002)
    uploads, downloads = {}, {}
    routes, route_index = {}, {}
    allocator = _FidAllocator((uploads, downloads, routes, route_index))
    primary = (1 << FID_LOW_BITS) | (allocator._epoch << 1)
    downstream = (2 << FID_LOW_BITS) | (allocator._epoch << 1)
    route = {"originator": ("127.0.0.1", 40000), "targets": {receiver: downstream}}
    routes[primary] = route
    route_index[(receiver, downstream)] = primary

    allocated = allocator.alloc(receiver=receiver)
    assert allocated not in (primary, downstream)

    # The child id is occupied only in its receiver scope; the upstream route
    # remains globally occupied regardless of the requested receiver.
    allocator._counter = 0
    assert allocator.alloc(receiver=other_receiver) == downstream
