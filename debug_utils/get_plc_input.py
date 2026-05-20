"""
TCP Listener for S7-1215C Input Monitor
========================================
Listens on PORT 11451, receives 100-byte input bitmap from PLC,
parses and displays which I/O bits have changed.

PLC sends: 25 DWords (little-endian) = 100 bytes = 800 bits
  Byte 0-3   鈫? DWord 1  鈫? I0.0..I3.7
  Byte 4-7   鈫? DWord 2  鈫? I4.0..I7.7
  ...
  Byte 96-99 鈫? DWord 25 鈫? I96.0..I99.7
"""

import socket
import struct
import datetime

PORT = 11451
HOST = "0.0.0.0"

def parse_bitmap(data: bytes):
    """Parse 100 bytes of input bitmap. Returns list of (addr, value) for set bits."""
    results = []
    for byte_idx in range(min(100, len(data))):
        byte_val = data[byte_idx]
        if byte_val == 0:
            continue
        for bit in range(8):
            if byte_val & (1 << bit):
                input_addr = byte_idx * 8 + bit   # e.g. 0鈫扞0.0, 1鈫扞0.1, ..., 8鈫扞1.0
                results.append((input_addr, 1))
    return results


def format_addr(addr: int) -> str:
    """Convert linear bit index to I{byte}.{bit} format."""
    byte = addr // 8
    bit = addr % 8
    return f"I{byte}.{bit}"


def main():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] TCP Listener starting on {HOST}:{PORT}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(1)
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Waiting for PLC connection...")

        prev_bits = set()

        while True:
            conn, addr = server.accept()
            print(f"\n[+] PLC connected from {addr[0]}:{addr[1]}")

            with conn:
                while True:
                    try:
                        # PLC may send 256 bytes (TSEND LEN) or 100 bytes 鈥? recv handles both
                        data = conn.recv(4096)
                        if not data:
                            print("[-] PLC disconnected")
                            break

                        ts = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]

                        if len(data) >= 100:
                            # Parse the first 100 bytes (25 DWords = 800 inputs)
                            bits = parse_bitmap(data[:100])
                            current_bits = {addr for addr, val in bits}

                            new_high = current_bits - prev_bits
                            new_low = prev_bits - current_bits

                            if new_high or new_low:
                                changes = []
                                for addr in sorted(new_high):
                                    changes.append(f"  鈫? {format_addr(addr)} 鈫? HIGH")
                                for addr in sorted(new_low):
                                    changes.append(f"  鈫? {format_addr(addr)} 鈫? LOW")

                                print(f"[{ts}] BITMAP UPDATE ({len(changes)} changes):")
                                for c in changes:
                                    print(c)
                            else:
                                print(f"[{ts}] Heartbeat (no changes, {len(bits)} inputs HIGH)")

                            prev_bits = current_bits
                        else:
                            print(f"[{ts}] Received {len(data)} bytes (too short, need >=100)")

                    except ConnectionResetError:
                        print("[-] Connection reset by PLC")
                        break
                    except Exception as e:
                        print(f"[!] Error: {e}")
                        break

if __name__ == "__main__":
    main()
