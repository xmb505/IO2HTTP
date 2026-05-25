"""
UDP Listener for S7-1215C Input Monitor
=======================================
Listens on UDP PORT 11451, receives 100-byte input bitmap from PLC.
No handshake, no heartbeat, no reconnection 鈥? just receive and display.

PLC sends via TUSEND: 25 DWords (little-endian) = 100 bytes = 800 bits
"""

import socket
import datetime

PORT = 11451
HOST = "0.0.0.0"


def parse_bitmap(data: bytes):
    """Parse 100 bytes of input bitmap. Returns set of active bit addresses."""
    results = set()
    for byte_idx in range(min(100, len(data))):
        byte_val = data[byte_idx]
        if byte_val == 0:
            continue
        for bit in range(8):
            if byte_val & (1 << bit):
                results.add(byte_idx * 8 + bit)
    return results


def format_addr(addr: int) -> str:
    """Convert linear bit index to I{byte}.{bit} format."""
    return f"I{addr // 8}.{addr % 8}"


def main():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] UDP Listener on {HOST}:{PORT}")
    print(f"[*] Fire-and-forget mode 鈥? no handshake, no heartbeat, no reconnect")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))

    prev_bits = set()

    while True:
        try:
            raw, addr = sock.recvfrom(4096)
            ts = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]

            if len(raw) < 100:
                print(f"[{ts}] Received {len(raw)} bytes from {addr[0]}:{addr[1]} (too short)")
                continue

            data = raw[:100]
            bits = parse_bitmap(data)
            current_bits = bits

            new_high = current_bits - prev_bits
            new_low = prev_bits - current_bits

            if new_high or new_low:
                changes = []
                for a in sorted(new_high):
                    changes.append(f"  -> {format_addr(a)} -> HIGH")
                for a in sorted(new_low):
                    changes.append(f"  -> {format_addr(a)} -> LOW")

                print(f"[{ts}] UDP UPDATE ({len(changes)} changes) from {addr[0]}:")
                for c in changes:
                    print(c)
            else:
                print(f"[{ts}] UDP packet ({len(bits)} HIGH) from {addr[0]}")

            prev_bits = current_bits

        except KeyboardInterrupt:
            print("\n[*] Shutting down")
            break
        except Exception as e:
            print(f"[!] Error: {e}")

    sock.close()


if __name__ == "__main__":
    main()

