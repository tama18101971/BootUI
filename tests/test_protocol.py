import pytest

import proteus32_protocol as proto
from conftest import BOOTLOADER_CLI, BOOTUI_ROOT, FakeSerial, load_module, read_frame, read_frame_echo

ACK = bytes.fromhex("AA55000055AA")
ERR_RANGE = bytes.fromhex("AA55000255AA")

ERASE_VECTOR = bytes.fromhex("AA55810000000008890055AA")
PROM_VECTOR = bytes.fromhex("AA558040") + b"\xFF" * 64 + bytes.fromhex("804055AA")
VERIFY_VECTOR = bytes.fromhex("AA55824000000008") + b"\xFF" * 64 + bytes.fromhex("8A4055AA")
END_VECTOR = bytes.fromhex("AA558300830055AA")
READ_VECTOR = bytes.fromhex("AA558404003F0008CF0055AA")
READ_RESPONSE_VECTOR = bytes.fromhex("AA558400") + b"\x00" * 64 + bytes.fromhex("840055AA")


@pytest.fixture(scope="session")
def bootui_cli():
    return load_module("bootui_proteus32_3", BOOTUI_ROOT / "proteus32_3.py")


@pytest.fixture(scope="session")
def bootloader_cli():
    if not BOOTLOADER_CLI.exists():
        pytest.skip("Proteus32-BootLoader repo not found next to BootUI")
    return load_module("bootloader_proteus32_3", BOOTLOADER_CLI)


@pytest.fixture(params=["bootui_cli", "bootloader_cli"])
def cli_client(request):
    return request.getfixturevalue(request.param)


# ---- TZ 3.5.4 control vectors: request frames (byte-for-byte) ----


def test_erase_vector():
    assert proto.make_packet(proto.CMD_IAP_ERASE) == ERASE_VECTOR


def test_prom_vector():
    assert proto.make_packet(proto.CMD_IAP_PROM, data=b"\xFF" * 64) == PROM_VECTOR


def test_verify_vector():
    assert proto.make_packet(proto.CMD_IAP_VERIFY, data=b"\xFF" * 64) == VERIFY_VECTOR


def test_end_vector():
    assert proto.make_packet(proto.CMD_IAP_END) == END_VECTOR


def test_read_vector():
    assert proto.make_packet(proto.CMD_IAP_READ, addr=0x08003F00) == READ_VECTOR


def test_unknown_command_rejected():
    with pytest.raises(ValueError):
        proto.make_packet(0x7F)


# ---- Base response parsing ----


def test_parse_ack():
    assert proto.parse_base_response(ACK) == (True, proto.ST_OK)


def test_parse_err_range():
    assert proto.parse_base_response(ERR_RANGE) == (False, proto.ST_ERR_RANGE)


@pytest.mark.parametrize("raw", [None, b"", b"AA", bytes.fromhex("AA55000055AB"), bytes.fromhex("AA55010055AA")])
def test_parse_garbage(raw):
    assert proto.parse_base_response(raw) == (False, None)


# ---- wait_read_response (the TZ 3.5.4 READ answer: AA 55 84 00 <64B> CK 55 AA) ----


def test_read_frame_helper_matches_spec_vector():
    assert read_frame(bytes(64)) == READ_RESPONSE_VECTOR


def test_read_response_exact_frame():
    data = bytes(range(64))
    fake = FakeSerial(rx=read_frame(data))
    got, err = proto.wait_read_response(fake)
    assert err is None
    assert got == data


def test_read_response_split_delivery():
    data = bytes(range(64))
    fake = FakeSerial(rx=read_frame(data), chunk_size=17)
    got, err = proto.wait_read_response(fake, timeout=0.5)
    assert err is None
    assert got == data


def test_read_response_addr_echo_variant():
    data = bytes(range(64))
    fake = FakeSerial(rx=read_frame_echo(data, 0x08003F00))
    got, err = proto.wait_read_response(fake)
    assert err is None
    assert got == data


def test_read_response_short_frame():
    fake = FakeSerial(rx=b"\xAA\x55\x84\x00" + b"\x00" * 40)
    got, err = proto.wait_read_response(fake, timeout=0.05)
    assert got is None
    assert "неполный" in err


def test_read_response_bad_checksum():
    frame = bytearray(read_frame(bytes(64)))
    frame[68] ^= 0xFF
    fake = FakeSerial(rx=bytes(frame))
    got, err = proto.wait_read_response(fake, timeout=0.05)
    assert got is None
    assert "контрольная сумма" in err


def test_read_response_bad_header():
    frame = bytearray(read_frame(bytes(64)))
    frame[2] = 0x85
    frame[68] ^= 0xFF
    fake = FakeSerial(rx=bytes(frame))
    got, err = proto.wait_read_response(fake, timeout=0.05)
    assert got is None
    assert "заголовок" in err


# ---- Transport: send_packet / send_end_best_effort / probe ----


def test_send_packet_ack_first_try():
    fake = FakeSerial([ACK])
    ok, status = proto.send_packet(fake, END_VECTOR)
    assert ok is True and status == proto.ST_OK
    assert len(fake.writes) == 1


def test_send_packet_error_not_retried():
    fake = FakeSerial([ERR_RANGE])
    ok, status = proto.send_packet(fake, ERASE_VECTOR, timeout=0.05)
    assert ok is False and status == proto.ST_ERR_RANGE
    assert len(fake.writes) == 1


def test_send_packet_silence_retries():
    fake = FakeSerial([])
    ok, status = proto.send_packet(fake, END_VECTOR, timeout=0.05)
    assert ok is False and status is None
    assert len(fake.writes) == proto.MAX_RETRIES


def test_send_read_packet_success_first_try():
    data = bytes(range(64))
    fake = FakeSerial([read_frame(data)])
    got, err = proto.send_read_packet(fake, proto.make_packet(proto.CMD_IAP_READ, addr=proto.FLASH_BASE))
    assert err is None
    assert got == data
    assert len(fake.writes) == 1


def test_send_read_packet_retries_then_succeeds():
    data = bytes(64)
    fake = FakeSerial([b"", read_frame(data)])
    got, err = proto.send_read_packet(fake, proto.make_packet(proto.CMD_IAP_READ, addr=proto.FLASH_BASE), timeout=0.05)
    assert err is None
    assert got == data
    assert len(fake.writes) == 2


def test_send_read_packet_silence():
    fake = FakeSerial([])
    got, err = proto.send_read_packet(fake, proto.make_packet(proto.CMD_IAP_READ, addr=proto.FLASH_BASE), timeout=0.05)
    assert got is None and err
    assert len(fake.writes) == proto.MAX_RETRIES


def test_end_best_effort_sends_vector_and_acks():
    fake = FakeSerial([ACK])
    ok, status = proto.send_end_best_effort(fake)
    assert ok is True and status == proto.ST_OK
    assert fake.writes == [END_VECTOR]


def test_end_best_effort_silence_ok():
    fake = FakeSerial([])
    ok, status = proto.send_end_best_effort(fake, timeout=0.05)
    assert ok is False and status is None


def test_end_best_effort_uses_timeout_cmd():
    import inspect

    assert inspect.signature(proto.send_end_best_effort).parameters["timeout"].default == proto.TIMEOUT_CMD


def test_probe_rev2_answers():
    fake = FakeSerial([read_frame(bytes(64))])
    data, err = proto.probe_protocol_v2(fake)
    assert err is None and data == bytes(64)
    assert fake.writes == [proto.make_packet(proto.CMD_IAP_READ, addr=proto.FLASH_BASE)]


def test_probe_rev1_silence():
    fake = FakeSerial([])
    data, err = proto.probe_protocol_v2(fake, timeout=0.05)
    assert data is None and err


# ---- Both CLI clients share the module protocol ----


def test_cli_matches_module_vectors(cli_client):
    assert cli_client.make_packet(cli_client.CMD_IAP_ERASE) == ERASE_VECTOR
    assert cli_client.make_packet(cli_client.CMD_IAP_PROM, data=b"\xFF" * 64) == PROM_VECTOR
    assert cli_client.make_packet(cli_client.CMD_IAP_VERIFY, data=b"\xFF" * 64) == VERIFY_VECTOR
    assert cli_client.make_packet(cli_client.CMD_IAP_END) == END_VECTOR
    assert cli_client.make_packet(cli_client.CMD_IAP_READ, addr=0x08003F00) == READ_VECTOR
    assert cli_client.ST_DICT == proto.ST_DICT
    assert getattr(cli_client, "TIMEOUT_CMD", proto.TIMEOUT_CMD) == proto.TIMEOUT_CMD
    assert getattr(cli_client, "MAX_RETRIES", proto.MAX_RETRIES) == proto.MAX_RETRIES


def test_bootloader_cli_read_fix(bootloader_cli):
    fake = FakeSerial(rx=read_frame(bytes(range(64))))
    got, err = bootloader_cli.wait_read_response(fake)
    assert err is None
    assert got == bytes(range(64))


def test_end_timeout_fixed_in_cli(cli_client):
    import inspect

    default = inspect.signature(cli_client.send_end_best_effort).parameters["timeout"].default
    assert default == getattr(cli_client, "TIMEOUT_CMD", proto.TIMEOUT_CMD)


# ---- End-to-end over the mocked serial port ----


def test_cmd_dump_end_to_end(cli_client, tmp_path, monkeypatch):
    data = bytes(range(256))
    fake = FakeSerial([read_frame(data[i:i + 64]) for i in range(0, 256, 64)])
    monkeypatch.setattr(cli_client, "open_port", lambda port, baud: fake)
    out = tmp_path / "dump.bin"

    rc = cli_client.cmd_dump("COM1", 460800, str(out), 0x08000000, 256)

    assert rc == 0
    assert out.read_bytes() == data
    for i, written in enumerate(fake.writes):
        addr = 0x08000000 + i * 64
        assert written == cli_client.make_packet(cli_client.CMD_IAP_READ, addr=addr)


def test_cmd_dump_reads_tz_vector_block(cli_client, tmp_path, monkeypatch):
    fake = FakeSerial([read_frame(bytes(64))])
    monkeypatch.setattr(cli_client, "open_port", lambda port, baud: fake)
    out = tmp_path / "dump.bin"

    rc = cli_client.cmd_dump("COM1", 460800, str(out), 0x08003F00, 64)

    assert rc == 0
    assert fake.writes[0] == READ_VECTOR


def test_cmd_dump_rejects_bad_args(cli_client, tmp_path, monkeypatch):
    fake = FakeSerial([])
    monkeypatch.setattr(cli_client, "open_port", lambda port, baud: fake)
    out = tmp_path / "dump.bin"

    assert cli_client.cmd_dump("COM1", 460800, str(out), 0x08000000, 0) == 1
    assert cli_client.cmd_dump("COM1", 460800, str(out), 0x08000003, 64) == 1
    assert cli_client.cmd_dump("COM1", 460800, str(out), 0x08003FF0, 64) == 1
    assert fake.writes == []


def test_cmd_write_full_cycle_vectors(cli_client, tmp_path, monkeypatch):
    fw = b"\xFF" * 128
    fake = FakeSerial([read_frame(bytes(64)), ACK, ACK, ACK, ACK, ACK, ACK])
    monkeypatch.setattr(cli_client, "open_port", lambda port, baud: fake)
    fw_path = tmp_path / "fw.bin"
    fw_path.write_bytes(fw)

    rc = cli_client.cmd_write("COM1", str(fw_path), 460800, True)

    assert rc == 0
    assert fake.writes[0] == cli_client.make_packet(cli_client.CMD_IAP_READ, addr=cli_client.FLASH_BASE)
    assert fake.writes[1] == ERASE_VECTOR
    assert fake.writes[2] == PROM_VECTOR
    assert fake.writes[3] == PROM_VECTOR
    assert fake.writes[4] == VERIFY_VECTOR
    assert fake.writes[5] == cli_client.make_packet(
        cli_client.CMD_IAP_VERIFY, addr=cli_client.FLASH_BASE + 64, data=b"\xFF" * 64
    )
    assert fake.writes[6] == END_VECTOR


def test_cmd_write_rejects_empty_firmware(cli_client, tmp_path, monkeypatch):
    fake = FakeSerial([read_frame(bytes(64))])
    monkeypatch.setattr(cli_client, "open_port", lambda port, baud: fake)
    fw_path = tmp_path / "empty.bin"
    fw_path.write_bytes(b"")

    assert cli_client.cmd_write("COM1", str(fw_path), 460800, True) == 1
    assert fake.writes == []


def test_cmd_write_rejects_oversize(cli_client, tmp_path, monkeypatch):
    fake = FakeSerial([read_frame(bytes(64))])
    monkeypatch.setattr(cli_client, "open_port", lambda port, baud: fake)
    fw_path = tmp_path / "big.bin"
    fw_path.write_bytes(b"\x00" * (cli_client.MAX_FLASH_SIZE + 1))

    assert cli_client.cmd_write("COM1", str(fw_path), 460800, True) == 1
    assert fake.writes == []


# ---- GUI client is wired to the shared module ----


def test_gui_uses_shared_protocol():
    import bootloader_gui

    assert bootloader_gui.make_packet is proto.make_packet
    assert bootloader_gui.send_packet is proto.send_packet
    assert bootloader_gui.send_end_best_effort is proto.send_end_best_effort
    assert bootloader_gui.probe_protocol_v2 is proto.probe_protocol_v2
    assert bootloader_gui.open_port is proto.open_port
    assert bootloader_gui.BOOTLOADER_BAUD == 460800
    assert bootloader_gui.PROTOCOL_REV == proto.PROTOCOL_REV
