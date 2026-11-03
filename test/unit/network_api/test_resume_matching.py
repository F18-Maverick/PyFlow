"""Unit tests for the receiver's ``/file`` resume matching (``_resumable_entry``).

A ``/file <name> <fid>`` line continues an interrupted download only when the
fid, the sender and the file name all match the progress entry. Anything else is
a new transfer and must never be matched to an existing partial file.
"""

import pytest

from PyFlow.network_api.connect_tcp import (
    _resumable_entry,
    format_resume_line,
    parse_resume_command,
)

PEER = ("127.0.0.1", 6000)


def _progress(peer=PEER, path="/recv/download.bin"):
    return {7: [1024, 4096, None, path, peer]}


def test_matching_fid_peer_and_name_resumes():
    progress = _progress()
    entry = progress[7]
    assert _resumable_entry(progress, 7, PEER, ["/file", "download.bin", "7"]) is entry


def test_name_is_compared_by_basename_of_the_partial_path():
    progress = _progress(path="/recv/nested/dir/download.bin")
    assert _resumable_entry(progress, 7, PEER, ["/file", "download.bin", "7"]) is progress[7]


def test_persisted_peer_list_matches_a_peer_tuple():
    # the progress file stores the peer as a JSON list; the live line carries a tuple
    progress = _progress(peer=["127.0.0.1", 6000])
    assert _resumable_entry(progress, 7, PEER, ["/file", "download.bin", "7"]) is progress[7]


@pytest.mark.parametrize(
    ("progress", "fid", "peer", "command_part"),
    [
        pytest.param(_progress(), 8, PEER, ["/file", "download.bin", "8"], id="unknown-fid"),
        pytest.param(
            _progress(), 7, ("10.0.0.2", 6000), ["/file", "download.bin", "7"], id="other-peer"
        ),
        pytest.param(_progress(), 7, PEER, ["/file", "other.bin", "7"], id="other-file-name"),
        pytest.param(
            _progress(peer=None), 7, PEER, ["/file", "download.bin", "7"], id="no-recorded-peer"
        ),
        pytest.param(_progress(), None, PEER, ["/file", "download.bin"], id="no-fid"),
        pytest.param(_progress(), 7, None, ["/file", "download.bin", "7"], id="no-peer"),
        pytest.param(
            _progress(), 7, PEER, ["/file_folder", "download.bin", "7"], id="folder-command"
        ),
        pytest.param(_progress(), 7, PEER, ["/file", "7"], id="too-few-tokens"),
    ],
)
def test_anything_else_is_a_new_transfer(progress, fid, peer, command_part):
    assert _resumable_entry(progress, fid, peer, command_part) is None


def test_resume_command_preserves_shell_quoted_endpoint_identity():
    origin = ("sender's host", 50001)
    target = ("target host", 50002)
    line = format_resume_line(304, 12345, origin, target)
    assert parse_resume_command(line) == (304, 12345, origin, target)


@pytest.mark.parametrize("line", [
    "/resume_trans 304 1",
    "/forward_resume '(\"host\", 5)' 304 1",
    "/resume_trans 304 -1 '(\"host\", 5)' '(\"target\", 6)'",
    "/resume_trans -1 1 '(\"host\", 5)' '(\"target\", 6)'",
    "/resume_trans 304 1 '[\"host\", 5]' '(\"target\", 6)'",
    "/resume_trans 304 1 '(\"host\", \"5\")' '(\"target\", 6)'",
    "/resume_trans 304 1 '(\"host\", 5)' '(\"target\",)'",
    "/resume_trans 304 1 '(\"host\", 5)' '(\"target\", 6)' extra",
    "/resume_trans 304 1 'unterminated",
])
def test_resume_command_rejects_ambiguous_or_malformed_identity(line):
    assert parse_resume_command(line) is None
