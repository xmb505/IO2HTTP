#!/usr/bin/env python3
"""
IO2KEYBOARD - WebSocket GPIO → 键盘映射工具

连接 IO2HTTP 的 WebSocket 服务器，监听 gpio_change 事件，
将 GPIO 输入状态变化映射为 Linux 键盘按键事件。

用法:
    python ws_to_keyboard.py                           # 使用默认配置
    python ws_to_keyboard.py -c my_keymap.json         # 指定配置文件
    python ws_to_keyboard.py -H 192.168.1.8 -p 8081   # 指定服务器地址
"""

import socket
import hashlib
import base64
import json
import time
import os
import re
import argparse
import signal
import sys
import string
import random
import threading
import struct

from evdev import UInput, ecodes


# ============================================================
#  配置加载
# ============================================================

def load_config(config_path):
    """加载按键映射配置 JSON 文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # JSON 标准不支持注释，用 _comment 作为元数据保留
    if '_comment' in config:
        del config['_comment']

    mapping = config.get('mapping', {})
    if not mapping:
        print("[警告] 配置文件中没有定义任何映射关系")
    return config


# ============================================================
#  WebSocket 客户端 (RFC 6455, 零依赖实现)
# ============================================================

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def _ws_generate_key():
    """生成随机的 Sec-WebSocket-Key"""
    raw = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    return base64.b64encode(raw.encode()).decode()


def ws_connect(host, port, path='/'):
    """建立 WebSocket 连接并完成 RFC 6455 握手，返回已连接的 socket"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)

    try:
        sock.connect((host, port))
    except (ConnectionRefusedError, socket.timeout):
        raise ConnectionError(f"无法连接到 ws://{host}:{port}")

    key = _ws_generate_key()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.send(request.encode())

    response = sock.recv(4096).decode('utf-8', errors='ignore')
    if '101' not in response.split('\r\n')[0]:
        raise ConnectionError(f"WebSocket 握手失败:\n{response[:300]}")

    # 验证 Accept (可选，但建议做)
    expected_accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()
    ).decode()
    for line in response.split('\r\n'):
        if line.startswith('Sec-WebSocket-Accept:'):
            actual_accept = line.split(':', 1)[1].strip()
            if actual_accept != expected_accept:
                sock.close()
                raise ConnectionError("Sec-WebSocket-Accept 校验失败")

    return sock


# WebSocket 超时 sentinel，区分 "没收到数据" 和 "连接断开"
_TIMEOUT_SENTINEL = object()


def recv_ws_frame(sock):
    """
    读取一个完整的 WebSocket 帧，返回解码后的字符串。
    服务器 → 客户端的帧不需要mask，所以直接读取payload。

    Returns:
        str | None | object: 文本帧 / 连接断开 None / 超时 _TIMEOUT_SENTINEL
    """
    # --- 读前 2 字节 ---
    header = _read_exact(sock, 2)
    if header is None:
        return None
    if header is _TIMEOUT_SENTINEL:
        return _TIMEOUT_SENTINEL

    fin_and_opcode = header[0]
    mask_and_len = header[1]

    opcode = fin_and_opcode & 0x0F

    # 处理控制帧
    if opcode == 0x8:   # Close
        return None
    if opcode == 0x9:   # Ping → 回复 Pong (RFC 6455 §5.5.2)
        payload_len = mask_and_len & 0x7F
        if mask_and_len & 0x80:
            mask_key = _read_exact(sock, 4)
            if mask_key is None or mask_key is _TIMEOUT_SENTINEL:
                return None
        else:
            mask_key = None
        ping_data = _read_exact(sock, payload_len)
        if ping_data is None or ping_data is _TIMEOUT_SENTINEL:
            return None
        if mask_key:
            ping_data = _apply_mask(ping_data, mask_key)
        # 回复 Pong（服务器→客户端帧不需要 mask）
        pong_frame = bytes([0x8A, payload_len]) + ping_data
        try:
            sock.sendall(pong_frame)
        except Exception:
            return None
        return _TIMEOUT_SENTINEL
    if opcode == 0xA:   # Pong
        return _TIMEOUT_SENTINEL

    if opcode not in (0x1, 0x2):  # 只处理 text/binary
        return _TIMEOUT_SENTINEL

    # --- 解析长度 ---
    payload_len = mask_and_len & 0x7F
    if payload_len == 126:
        ext = _read_exact(sock, 2)
        if ext is None:
            return None
        if ext is _TIMEOUT_SENTINEL:
            return _TIMEOUT_SENTINEL
        payload_len = int.from_bytes(ext, 'big')
    elif payload_len == 127:
        ext = _read_exact(sock, 8)
        if ext is None:
            return None
        if ext is _TIMEOUT_SENTINEL:
            return _TIMEOUT_SENTINEL
        payload_len = int.from_bytes(ext, 'big')

    # --- 读 payload (服务器帧不应mask) ---
    payload = _read_exact(sock, payload_len)
    if payload is None:
        return None
    if payload is _TIMEOUT_SENTINEL:
        return _TIMEOUT_SENTINEL

    return payload.decode('utf-8')


def _read_exact(sock, n):
    """从 socket 精确读取 n 字节"""
    data = b''
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
        except socket.timeout:
            return _TIMEOUT_SENTINEL    # 区分超时和真正断开
        if not chunk:
            return None                 # 连接真正断开
        data += chunk
    return data


def _apply_mask(data, mask_key):
    """RFC 6455 unmasking"""
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))


# ============================================================
#  键盘模拟器
# ============================================================

class KeyMapper:
    """GPIO → 键盘按键映射器，通过 /dev/uinput 注入事件

    使用事件缓冲队列 + 滑动窗口 flush 机制，将 16ms 窗口内的
    多个 GPIO 变化合并为一个输入报告（一次 SYN_REPORT），
    确保多个按键被操作系统识别为"同时按下"（音游双押支持）。
    """

    def __init__(self, mapping_config, flush_interval=0.016):
        """
        Args:
            mapping_config: dict, {"I0.0": "KEY_A", "I0.1": "KEY_B", ...}
            flush_interval: 合并窗口（秒），默认 0.016，音游建议 0.004
        """
        self._flush_interval = flush_interval
        self.mapping = {}
        for gpio_name, key_name in mapping_config.items():
            keycode = getattr(ecodes, key_name, None)
            if keycode is None:
                print(f"[警告] 未知按键名 '{key_name}'，跳过 GPIO {gpio_name}")
                continue
            self.mapping[gpio_name] = keycode

        # keycode → gpio_name 反向映射（用于冲突检测）
        self._code_to_gpio = {v: k for k, v in self.mapping.items()}

        # 按键当前状态，用于防抖（bit=1 表示按下中）
        self._key_pressed = {}

        # 事件缓冲队列
        self._pending_events = []
        self._pending_lock = threading.Lock()
        self._flush_timer = None

        # 创建虚拟输入设备
        self._ui = UInput()
        self._print_mapping()

    def _key_name(self, code):
        """keycode → 可读名称（优先返回 KEY_ / BTN_ 前缀名称）"""
        fallback = None
        for name, val in ecodes.ecodes.items():
            if val == code:
                if name.startswith('KEY_') or name.startswith('BTN_'):
                    return name
                if fallback is None:
                    fallback = name
        return fallback if fallback else f"0x{code:X}"

    def _print_mapping(self):
        """输出映射表"""
        print(f"[键盘] 虚拟设备已创建，已加载 {len(self.mapping)} 个映射:")
        for gpio, code in self.mapping.items():
            print(f"  {gpio:>6s}  →  {self._key_name(code)}")

    def _schedule_flush(self):
        """注意：调用者必须在 _pending_lock 内调用此方法"""
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = threading.Timer(self._flush_interval, self._flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _write_events_atomic(self, events):
        """将多个事件 + SYN_REPORT 通过单次 write() syscall 写入 uinput，
        确保 kernel 给所有事件打上相同时间戳（模拟物理键盘 HID 报告）。"""
        if not events:
            return
        # struct input_event: llHHi (timeval + type + code + value) = 24 bytes
        buf = b''
        for ev_type, code, value in events:
            buf += struct.pack('llHHi', 0, 0, ev_type, code, value)
        # SYN_REPORT
        buf += struct.pack('llHHi', 0, 0, 0, 0, 0)
        os.write(self._ui.fd, buf)

    def _flush(self):
        """将缓冲队列中的事件发送到 uinput

        检测同批次内同一按键的 down+up 冲突：
        如果某按键在同一批次内既有 KEY_DOWN 又有 KEY_UP，
        只发送 KEY_DOWN，将 KEY_UP 推迟到下一批次。
        （避免游戏因 net state=up 忽略本次 tap）
        """
        with self._pending_lock:
            events = list(self._pending_events)
            self._pending_events.clear()
            self._flush_timer = None

            down_codes = set()
            up_codes = set()
            for ev_type, code, value in events:
                if value == 1:
                    down_codes.add(code)
                elif value == 0:
                    up_codes.add(code)
            conflict_codes = down_codes & up_codes

            if conflict_codes:
                keep = []
                defer = []
                for ev_type, code, value in events:
                    if code in conflict_codes and value == 0:
                        defer.append((ev_type, code, value))
                        # 推迟 KEY_UP：回退 _key_pressed 为 1，假装仍在按下
                        # 等延迟发送后再由下一次 PLC 扫描纠正状态
                        gpio = self._code_to_gpio.get(code)
                        if gpio:
                            self._key_pressed[gpio] = 1
                    else:
                        keep.append((ev_type, code, value))
                if defer:
                    self._pending_events = defer
                    self._flush_timer = threading.Timer(
                        self._flush_interval, self._flush
                    )
                    self._flush_timer.daemon = True
                    self._flush_timer.start()
                events = keep

            # 同步 _key_pressed：对即将写入的 KEY_UP 事件标记为已释放
            for ev_type, code, value in events:
                if value == 0:
                    gpio = self._code_to_gpio.get(code)
                    if gpio:
                        self._key_pressed[gpio] = 0

        self._write_events_atomic(events)

    def handle(self, changes):
        """
        处理 GPIO 变化列表。

        Args:
            changes: [{"gpio": "I0.0", "bit": 1}, ...]   PLC
                     或 [{"gpio": 1, "bit": 1}, ...]     USB2GPIO (整数)
        """
        with self._pending_lock:
            for item in changes:
                gpio = str(item['gpio'])
                bit = item['bit']

                if gpio not in self.mapping:
                    continue

                keycode = self.mapping[gpio]

                # 防止重复按/放
                if self._key_pressed.get(gpio) == bit:
                    continue
                self._key_pressed[gpio] = bit

                if bit == 1:
                    self._pending_events.append((ecodes.EV_KEY, keycode, 1))
                    print(f"  ↓ {self._key_name(keycode)}  (GPIO {gpio})")
                else:
                    self._pending_events.append((ecodes.EV_KEY, keycode, 0))
                    print(f"  ↑ {self._key_name(keycode)}  (GPIO {gpio})")

            self._schedule_flush()

    def release_all(self):
        """释放所有当前按下的按键（用于优雅退出）"""
        with self._pending_lock:
            if self._flush_timer:
                self._flush_timer.cancel()
                self._flush_timer = None
            self._pending_events.clear()

        # 收集所有需要释放的按键
        release_events = []
        for gpio, code in self.mapping.items():
            if self._key_pressed.get(gpio) == 1:
                release_events.append((ecodes.EV_KEY, code, 0))
        self._write_events_atomic(release_events)
        self._ui.close()
        print("[键盘] 已释放所有按键，设备关闭")


# ============================================================
#  主程序
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='IO2KEYBOARD - GPIO WebSocket → 键盘映射'
    )
    parser.add_argument('-c', '--config', default='keymap.json',
                        help='配置文件路径 (默认: keymap.json)')
    parser.add_argument('-H', '--host', default='127.0.0.1',
                        help='WebSocket 服务器地址 (默认: 127.0.0.1)')
    parser.add_argument('-p', '--port', type=int, default=8081,
                        help='WebSocket 服务器端口 (默认: 8081)')
    parser.add_argument('--list-keys', action='store_true',
                        help='列出所有可用的按键名后退出')

    args = parser.parse_args()

    # --list-keys
    if args.list_keys:
        print("可用的按键名 (evdev ecodes):")
        for name in sorted(ecodes.ecodes.keys()):
            if name.startswith('KEY_') or name.startswith('BTN_'):
                print(f"  {name}")
        print("\n提示: 大多数情况下使用 KEY_ 前缀的按键")
        return

    # 加载配置
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(script_dir, config_path)

    print(f"[配置] {config_path}")
    config = load_config(config_path)

    # 解析 WebSocket 地址（优先用配置文件中的 ws_url）
    ws_url = config.get('ws_url', '')
    if ws_url:
        m = re.match(r'ws://([^:/]+)(?::(\d+))?(/.*)?', ws_url)
        if m:
            ws_host = m.group(1)
            ws_port = int(m.group(2) or 8081)
            ws_path = m.group(3) or '/'
        else:
            print(f"[错误] 无效的 ws_url: {ws_url}")
            sys.exit(1)
    else:
        ws_host = args.host
        ws_port = args.port
        ws_path = '/'

    # 目标设备别名（强制）
    target_alias = config.get('alias', '')
    if not target_alias:
        print("[错误] 未配置 alias，请在 keymap.json 中指定要监听哪个设备")
        print('  "alias": "plc"     # 监听 PLC 输入 (I0.0, I0.1...)')
        print('  "alias": "geter"   # 监听 USB2GPIO 输入 (引脚 1, 2, 3...)')
        sys.exit(1)
    print(f"[配置] 监听设备别名: {target_alias}")

    # 创建键盘映射器
    mapper = KeyMapper(
        config.get('mapping', {}),
        flush_interval=config.get('flush_interval', 0.016),
    )

    # 信号处理
    running = True

    def on_exit(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    # 主循环：连接 → 监听 → 断线重连
    print(f"[连接] ws://{ws_host}:{ws_port}{ws_path}")
    retry_delay = 1

    while running:
        try:
            sock = ws_connect(ws_host, ws_port, ws_path)
            print("[连接] 已建立 WebSocket 连接")
            retry_delay = 1  # 重置重试间隔
            sock.settimeout(2)

            while running:
                try:
                    frame = recv_ws_frame(sock)
                except Exception:
                    break

                if frame is _TIMEOUT_SENTINEL:
                    # 超时：服务器只是没发数据，连接仍然正常
                    continue

                if frame is None:
                    print("[连接] 断开")
                    break

                # 解析 JSON
                try:
                    msg = json.loads(frame)
                except json.JSONDecodeError:
                    continue

                if msg.get('type') != 'gpio_change':
                    continue

                # 遍历设备变化，按 alias 过滤
                for gpio_info in msg.get('gpios', []):
                    alias = gpio_info.get('alias', '?')

                    # 如果配置了 target_alias，只处理匹配设备
                    if target_alias and alias != target_alias:
                        continue

                    changes = gpio_info.get('change_gpio', [])
                    if not changes:
                        continue

                    print(f"[事件] {alias}: {changes}")
                    mapper.handle(changes)

            try:
                sock.close()
            except Exception:
                pass

        except ConnectionError as e:
            print(f"[错误] {e}")
        except Exception as e:
            print(f"[错误] {e}")

        if running:
            print(f"[重连] {retry_delay}秒后重试...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)

    # 退出清理
    mapper.release_all()
    print("[退出] IO2KEYBOARD 已停止")


if __name__ == '__main__':
    main()
