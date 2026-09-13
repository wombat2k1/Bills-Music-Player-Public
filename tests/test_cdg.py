import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from billsmusic.cdg import CdgDocument


def _packet(instruction, payload=b""):
    packet = bytearray(24)
    packet[0] = 0x09
    packet[1] = instruction
    packet[4:4 + min(16, len(payload))] = payload[:16]
    return bytes(packet)


def _colour_table(index, red, green, blue):
    colours = [0] * 8
    colours[index] = ((red & 15) << 8) | ((green & 15) << 4) | (blue & 15)
    payload = bytearray()
    for value in colours:
        payload.extend(((value >> 6) & 0x3F, value & 0x3F))
    return _packet(30, payload)


def test_memory_preset_colour_and_seek_are_deterministic():
    data = _colour_table(1, 15, 0, 0) + _packet(1, bytes([1])) + _packet(1, bytes([0]))
    document = CdgDocument(data, checkpoint_packets=300)

    red = document.image_at(7).pixelColor(10, 10)
    black = document.image_at(11).pixelColor(10, 10)
    red_again = document.image_at(7).pixelColor(10, 10)

    assert (red.red(), red.green(), red.blue()) == (255, 0, 0)
    assert (black.red(), black.green(), black.blue()) == (0, 0, 0)
    assert red_again == red


def test_tile_xor_and_transparency_commands_render_safely():
    tile = bytes([0, 1, 1, 1] + [0x3F] * 12)
    xor_tile = bytes([0, 1, 1, 1] + [0x3F] * 12)
    data = (
        _colour_table(1, 0, 15, 0)
        + _packet(6, tile)
        + _packet(38, xor_tile)
        + _packet(28, bytes([0]))
    )
    document = CdgDocument(data)

    image = document.image_at(20)

    assert image.width() == 288
    assert image.height() == 192
    assert image.pixelColor(0, 0).alpha() == 0


def test_invalid_and_trailing_cdg_data_is_ignored_without_crashing():
    document = CdgDocument(b"bad packet data" + bytes(24))
    image = document.image_at(1000)
    assert not image.isNull()
    assert image.size().width() == 288
    # The garbage leading bytes don't form a valid 0x09 command packet --
    # counted for the aggregate playback.cdg_parse_warning diagnostic
    # (window.py), never crashes, never logged per-packet.
    assert document.unrecognized_packet_count >= 1


def test_sequential_forward_playback_replays_from_last_frame_not_checkpoint():
    # Normal playback calls image_at() every tick with a slowly advancing
    # position -- this must replay only the packets since the last
    # rendered frame, not from the nearest checkpoint each time (that was
    # measured at ~36ms/call, a real GUI-thread stutter risk).
    data = _colour_table(1, 15, 0, 0) + _packet(1, bytes([1])) * 1000
    document = CdgDocument(data, checkpoint_packets=300)

    document.image_at(500)
    assert document._last_render.packet == 150
    document.image_at(600)
    assert document._last_render.packet == 180
    # Confirms the replay window is small (only packets 150..180), not
    # reset back to the checkpoint at packet 0 on every call.


def test_backward_seek_after_forward_playback_falls_back_to_checkpoint():
    data = _colour_table(1, 15, 0, 0) + _packet(1, bytes([1])) + _packet(1, bytes([0]))
    document = CdgDocument(data, checkpoint_packets=300)

    document.image_at(1000)
    black = document.image_at(11).pixelColor(10, 10)
    red_again = document.image_at(7).pixelColor(10, 10)

    assert (black.red(), black.green(), black.blue()) == (0, 0, 0)
    assert (red_again.red(), red_again.green(), red_again.blue()) == (255, 0, 0)
