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
