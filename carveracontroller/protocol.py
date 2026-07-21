"""
Framed binary protocol used by the Makera Z1 / Z1 Pro.

The original Carvera and Carvera Air (and the Community firmware) speak a plain,
newline-terminated command protocol with classic XMODEM file transfer. The Z1
firmware instead wraps every command and every response in a binary frame and
reframes file transfer on top of the same envelope. This module implements that
framing so the controller can talk to a Z1; the raw protocol path is untouched.

Frame layout (all multi-byte fields big-endian):

    [0x8668][DATA_LENGTH:2][PTYPE:1][payload...][CRC16:2][0x55AA]

    DATA_LENGTH = 1 + len(payload) + 2      (ptype + payload + crc)
    CRC16       = CRC-16/XMODEM (poly 0x1021, init 0x0000)
                  computed over DATA_LENGTH + PTYPE + payload

Reverse-engineered from the Makera Z1 controller; CRC table verified against it.
"""

import math
import struct
import time

FRAME_HEADER = 0x8668
FRAME_END = 0x55AA
MAX_DATA_LEN = 8200          # firmware rejects a DATA_LENGTH larger than this

# Outbound packet types
PTYPE_CTRL_SINGLE = 0xA1     # single realtime char, e.g. '?', '!', '~'
PTYPE_CTRL_MULTI = 0xA2      # a command line / gcode
PTYPE_FILE_START = 0xB0      # 'upload'/'download' text command
PTYPE_FILE_QUERY = 0xB7      # query currently playing file

# Inbound packet types
PTYPE_STATUS_RES = 0x81
PTYPE_DIAG_RES = 0x82
PTYPE_LOAD_INFO = 0x83
PTYPE_LOAD_FINISH = 0x84
PTYPE_LOAD_ERROR = 0x85
PTYPE_NORMAL_INFO = 0x90
PTYPE_ALARM_INFO = 0x91

# File-transfer packet types (both directions)
PTYPE_FILE_MD5 = 0xB1
PTYPE_FILE_VIEW = 0xB2
PTYPE_FILE_DATA = 0xB3
PTYPE_FILE_END = 0xB4
PTYPE_FILE_CAN = 0xB5
PTYPE_FILE_RETRY = 0xB6

FILE_PACKET_SIZE = 8192


def _build_crc_table():
    table = []
    for i in range(256):
        crc = 0
        c = i << 8
        for _ in range(8):
            if (crc ^ c) & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
            c = (c << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC_TABLE = _build_crc_table()


def crc16(data):
    crc = 0
    for b in data:
        crc = (((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc


def build_frame(ptype, payload=b''):
    if isinstance(payload, str):
        payload = payload.encode('utf-8', errors='replace')
    data_length = 1 + len(payload) + 2
    body = data_length.to_bytes(2, 'big') + bytes([ptype]) + payload
    crc = crc16(body)
    return (FRAME_HEADER.to_bytes(2, 'big') + body +
            crc.to_bytes(2, 'big') + FRAME_END.to_bytes(2, 'big'))


def encode_command(line):
    """A command line / gcode. The frame delimits it, so no trailing newline."""
    if isinstance(line, str):
        line = line.encode('utf-8', errors='replace')
    return build_frame(PTYPE_CTRL_MULTI, line.rstrip(b'\r\n'))


def encode_realtime(value):
    """A single realtime control byte (int or 1-byte bytes/str)."""
    if isinstance(value, str):
        value = value.encode('latin-1')
    if isinstance(value, (bytes, bytearray)):
        value = value[0]
    return build_frame(PTYPE_CTRL_SINGLE, bytes([value]))


def encode_file_command(line):
    """A file-transfer text command ('upload ...' / 'download ...')."""
    if isinstance(line, str):
        line = line.encode('utf-8', errors='replace')
    return build_frame(PTYPE_FILE_START, line)


# Payload types whose text records carry a trailing terminator byte the firmware
# appends and the vendor app strips before parsing.
_TEXT_TYPES = (PTYPE_STATUS_RES, PTYPE_DIAG_RES, PTYPE_NORMAL_INFO, PTYPE_LOAD_INFO)


def payload_text(ptype, payload):
    """Decode a text payload, dropping the firmware's trailing terminator."""
    if ptype in _TEXT_TYPES and payload.endswith((b'\n', b'\r', b'\x00')):
        payload = payload[:-1]
    return payload.decode('utf-8', errors='ignore')


class FrameDecoder:
    """Incremental decoder: feed raw bytes, get back complete, CRC-checked frames."""

    _WAIT_HEADER, _READ_LENGTH, _READ_DATA, _CHECK_FOOTER = range(4)

    def __init__(self):
        self.reset()

    def reset(self):
        self._state = self._WAIT_HEADER
        self._buf = bytearray()
        self._packet = bytearray()   # [lenH][lenL][ptype][payload][crcH][crcL]
        self._expected = 0

    def feed(self, data):
        """Return a list of (ptype, payload) for every complete valid frame."""
        frames = []
        for b in data:
            if self._state == self._WAIT_HEADER:
                self._buf.append(b)
                if len(self._buf) > 2:
                    del self._buf[0]
                if len(self._buf) == 2 and (self._buf[0] << 8 | self._buf[1]) == FRAME_HEADER:
                    self._buf.clear()
                    self._packet.clear()
                    self._state = self._READ_LENGTH
            elif self._state == self._READ_LENGTH:
                self._packet.append(b)
                if len(self._packet) == 2:
                    self._expected = self._packet[0] << 8 | self._packet[1]
                    if not (0 <= self._expected <= MAX_DATA_LEN):
                        self.reset()
                    else:
                        self._state = self._READ_DATA
            elif self._state == self._READ_DATA:
                self._packet.append(b)
                if len(self._packet) == 2 + self._expected:
                    self._state = self._CHECK_FOOTER
                    self._buf.clear()
            elif self._state == self._CHECK_FOOTER:
                self._buf.append(b)
                if len(self._buf) == 2:
                    frame = self._parse_complete()
                    if frame is not None:
                        frames.append(frame)
                    self.reset()
        return frames

    def _parse_complete(self):
        footer = self._buf[0] << 8 | self._buf[1]
        if footer != FRAME_END or len(self._packet) < 5:
            return None
        crc_rx = self._packet[-2] << 8 | self._packet[-1]
        if crc16(self._packet[:-2]) != crc_rx:
            return None
        ptype = self._packet[2]
        payload = bytes(self._packet[3:-2])
        return (ptype, payload)


def recv_packet(getc, timeout):
    """Read one complete frame using an XMODEM-style getc(size, timeout).

    Returns (ptype, payload) on a valid frame, or None on timeout/short/corrupt.
    """
    deadline = time.time() + timeout

    def read_exact(n):
        buf = bytearray()
        while len(buf) < n and time.time() < deadline:
            chunk = getc(n - len(buf), max(0.01, deadline - time.time()))
            if chunk:
                buf.extend(chunk)
        return bytes(buf) if len(buf) == n else None

    # sync on header
    window = bytearray()
    while time.time() < deadline:
        c = getc(1, max(0.01, deadline - time.time()))
        if not c:
            continue
        window.extend(c)
        if len(window) > 2:
            del window[0]
        if len(window) == 2 and (window[0] << 8 | window[1]) == FRAME_HEADER:
            break
    else:
        return None

    length_bytes = read_exact(2)
    if length_bytes is None:
        return None
    expected = length_bytes[0] << 8 | length_bytes[1]
    if not (0 <= expected <= MAX_DATA_LEN):
        return None
    body = read_exact(expected)          # [ptype][payload][crc]
    footer = read_exact(2)
    if body is None or footer is None:
        return None
    if (footer[0] << 8 | footer[1]) != FRAME_END:
        return None
    packet = length_bytes + body
    crc_rx = packet[-2] << 8 | packet[-1]
    if crc16(packet[:-2]) != crc_rx or len(packet) < 5:
        return None
    return (packet[2], bytes(packet[3:-2]))


class FramedFileTransfer:
    """File upload/download over the framed protocol (replaces XMODEM for the Z1).

    Driven by the machine: for both directions the machine requests each block by
    sequence number. Sequence numbers are 1-based; the last data block is short
    (no padding). Effective retry budget and timeouts match the vendor client.
    """

    def __init__(self, getc, putc):
        self.getc = getc
        self.putc = putc
        self.canceled = False

    def _send(self, ptype, payload=b''):
        self.putc(build_frame(ptype, payload))

    def send(self, stream, md5, retry=50, callback=None):
        """Upload a local file object to the machine.

        Returns True on success, None on cancel/alarm/timeout.
        """
        stream.seek(0, 2)
        file_size = stream.tell()
        stream.seek(0, 0)
        total_packets = int(math.ceil(file_size / FILE_PACKET_SIZE)) or 1

        self._send(PTYPE_FILE_MD5, (md5 or '').encode('utf-8'))
        last_seq = 0
        last_data = b''
        td = time.time()

        while True:
            if self.canceled:
                self._send(PTYPE_FILE_CAN)
                self.canceled = False
                return None

            pkt = recv_packet(self.getc, 40)
            if pkt is None:
                if time.time() - td > 9:
                    self._send(PTYPE_FILE_CAN)
                    return None
                continue

            ptype, payload = pkt
            td = time.time()

            if ptype == PTYPE_ALARM_INFO:
                return None
            if ptype < PTYPE_FILE_MD5:
                continue

            if ptype == PTYPE_FILE_MD5:
                self._send(PTYPE_FILE_MD5, (md5 or '').encode('utf-8'))
            elif ptype == PTYPE_FILE_VIEW:
                packetno = int(math.ceil(file_size / FILE_PACKET_SIZE)) or 1
                self._send(PTYPE_FILE_VIEW,
                           struct.pack('>I', packetno) + struct.pack('>H', FILE_PACKET_SIZE))
                last_seq = 0
            elif ptype == PTYPE_FILE_DATA:
                seq = struct.unpack('>I', payload[:4])[0]
                if seq == last_seq:
                    self._send(PTYPE_FILE_DATA, last_data)
                else:
                    if seq != last_seq + 1:
                        stream.seek((seq - 1) * FILE_PACKET_SIZE, 0)
                    block = stream.read(FILE_PACKET_SIZE)
                    last_data = struct.pack('>I', seq) + block
                    self._send(PTYPE_FILE_DATA, last_data)
                last_seq = seq
                if callback:
                    # community uploadCallback(packet_size, total_packets, success_count, error_count)
                    callback(FILE_PACKET_SIZE, total_packets, seq, 0)
            elif ptype == PTYPE_FILE_END:
                return True
            elif ptype == PTYPE_FILE_CAN:
                return None

    def recv(self, stream, md5, retry=50, callback=None):
        """Download a file from the machine into a local file object.

        Returns bytes written (>0) on success, 0 if skipped (md5 match),
        -1 on user cancel, None on machine cancel/alarm/retry-exhausted.
        """
        WAIT_MD5, WAIT_VIEW, READ_DATA = range(3)
        state = WAIT_MD5
        sequence = 0
        total_packet = 0
        income_size = 0
        error_count = 0
        total_err = 0

        while True:
            if self.canceled:
                self._send(PTYPE_FILE_CAN)
                self.canceled = False
                return -1

            pkt = recv_packet(self.getc, 5)
            if pkt is None:
                error_count += 1
                total_err += 1
                self._send(PTYPE_FILE_RETRY)
                if total_err >= retry:
                    self._send(PTYPE_FILE_CAN)
                    return None
                continue

            ptype, payload = pkt
            if ptype == PTYPE_ALARM_INFO:
                return None
            if ptype < PTYPE_FILE_MD5:
                continue
            if ptype == PTYPE_FILE_CAN:
                return None

            if state == WAIT_MD5:
                if ptype == PTYPE_FILE_MD5:
                    device_md5 = payload.decode('utf-8', errors='ignore')
                    if md5 and md5 == device_md5:
                        self._send(PTYPE_FILE_CAN)
                        return 0
                    self._send(PTYPE_FILE_VIEW)
                    state = WAIT_VIEW
                    error_count = total_err = 0
                else:
                    error_count += 1
                    if error_count >= retry:
                        self._send(PTYPE_FILE_MD5)
                    total_err += 1
            elif state == WAIT_VIEW:
                if ptype == PTYPE_FILE_VIEW:
                    total_packet = struct.unpack('>I', payload[:4])[0]
                    sequence = 1
                    self._send(PTYPE_FILE_DATA, struct.pack('>I', sequence))
                    state = READ_DATA
                    error_count = total_err = 0
                else:
                    error_count += 1
                    if error_count >= retry:
                        self._send(PTYPE_FILE_VIEW)
                    total_err += 1
            elif state == READ_DATA:
                seq = struct.unpack('>I', payload[:4])[0]
                if ptype == PTYPE_FILE_DATA and seq == sequence:
                    file_bytes = payload[4:]
                    income_size += len(file_bytes)
                    stream.write(file_bytes)
                    if sequence < total_packet:
                        sequence += 1
                        self._send(PTYPE_FILE_DATA, struct.pack('>I', sequence))
                    if callback:
                        # community downloadCallback(packet_size, success_count, error_count)
                        callback(FILE_PACKET_SIZE, seq, 0)
                    error_count = total_err = 0
                    if seq == total_packet:
                        self._send(PTYPE_FILE_END)
                        return income_size
                else:
                    error_count += 1
                    total_err += 1
                    if error_count >= retry:
                        self._send(PTYPE_FILE_DATA, struct.pack('>I', sequence))
                    if total_err >= retry:
                        self._send(PTYPE_FILE_CAN)
                        return None
