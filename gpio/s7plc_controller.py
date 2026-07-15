#!/usr/bin/env python3
"""
S7 PLC 控制器模块
通过 snap7 库与西门子 S7 PLC 进行通信
支持 PLC 主动上报输入状态（TCP / UDP 监听）
"""

import re
import socket
import time
import threading

from snap7.client import Client
from snap7.type import Area
from snap7.util import set_bool, get_bool

from .base_controller import GPIOControllerBase

import random
import struct

# PLC IO 地址到 snap7 Area 的映射
IO_AREA_MAP = {
    'Q': Area.PA,   # 输出过程映像
    'I': Area.PE,   # 输入过程映像
    'M': Area.MK,   # 存储器区
    'DB': Area.DB,  # 数据块
}


class S7PLCController(GPIOControllerBase):
    """S7 PLC 控制器，通过 snap7 库与西门子 S7 PLC 通信"""

    def __init__(self, ip_address, port=102, rack=0, slot=1, read_port=0,
                 event_input_protocol='udp', simulate=False, debug=False):
        config = {
            'ip_address': ip_address,
            'port': port,
            'rack': rack,
            'slot': slot,
            'read_port': read_port,
            'event_input_protocol': event_input_protocol,
        }
        super().__init__(config, simulate=simulate, debug=debug)

        self.ip_address = ip_address
        self.port = port
        self.rack = rack
        self.slot = slot
        self.read_port = read_port
        self.event_input_protocol = (event_input_protocol or 'udp').lower()
        self.client = None
        self._lock = threading.Lock()  # PLC 操作锁

        # PLC 主动上报监听
        self.read_socket = None
        self.read_thread = None
        self.plc_input_last = {}  # 上次输入状态 {addr: state}
        self.input_callback = None  # 回调函数 (alias, changes)
        self.input_lock = threading.Lock()
        self._running = True  # 控制监听线程生命周期

        if not simulate:
            self.connect()
        else:
            print(f"S7 PLC 控制器运行在模拟模式，目标: {self.ip_address}")

    def set_input_callback(self, callback):
        """设置输入状态变化回调函数"""
        self.input_callback = callback

    def start_input_listener(self):
        """启动 PLC 主动上报监听线程"""
        if not self.read_port:
            print("未配置 read_port，跳过 PLC 输入监听")
            return

        proto = self.event_input_protocol.upper()
        self.read_thread = threading.Thread(
            target=self._listen_plc_input,
            daemon=True,
            name=f"PLC-Input-Listener-{proto}"
        )
        self.read_thread.start()
        print(f"PLC 输入监听已启动，监听端口 {self.read_port}，协议 {proto}")

    def stop_input_listener(self):
        """停止 PLC 输入监听"""
        if self.read_socket:
            try:
                self.read_socket.close()
            except:
                pass

    def _listen_plc_input(self):
        """根据 event_input_protocol 选择 TCP 或 UDP 监听"""
        if self.event_input_protocol == 'udp':
            self._listen_udp()
        else:
            self._listen_tcp()

    def _listen_tcp(self):
        """TCP 模式：PLC 通过 TCP 连接推送 100 字节位图，有连接管理"""
        try:
            self.read_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.read_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.read_socket.bind(('0.0.0.0', self.read_port))
            self.read_socket.listen(1)
            print(f"PLC 输入监听 TCP Socket 已绑定 0.0.0.0:{self.read_port}")

            while True:
                try:
                    conn, addr = self.read_socket.accept()
                    print(f"PLC 输入连接已建立: {addr[0]}:{addr[1]}")
                    with conn:
                        while True:
                            data = conn.recv(4096)
                            if not data:
                                print("PLC 输入连接已断开")
                                break

                            if len(data) >= 100:
                                changes, bitmap_hex = self._parse_input_bitmap(data[:100])
                                if self.input_callback:
                                    self.input_callback(changes, bitmap_hex)

                except ConnectionResetError:
                    print("PLC 连接被重置，等待重连")
                    continue
                except OSError as e:
                    if self._running:
                        print(f"PLC TCP 输入监听错误: {e}")
                    continue

        except Exception as e:
            print(f"PLC TCP 输入监听启动失败: {e}")

    def _listen_udp(self):
        """UDP 模式：PLC 通过 TUSEND 发送 UDP 数据包，无连接无握手"""
        try:
            self.read_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.read_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.read_socket.settimeout(2)  # 超时以便检测 _running 状态
            self.read_socket.bind(('0.0.0.0', self.read_port))
            print(f"PLC 输入监听 UDP Socket 已绑定 0.0.0.0:{self.read_port}")

            while self._running:
                try:
                    data, addr = self.read_socket.recvfrom(4096)
                except socket.timeout:
                    continue

                if len(data) >= 100:
                    changes, bitmap_hex = self._parse_input_bitmap(data[:100])
                    if self.input_callback:
                        self.input_callback(changes, bitmap_hex)

        except Exception as e:
            if self._running:
                print(f"PLC UDP 输入监听启动失败: {e}")

    def _parse_input_bitmap(self, data: bytes) -> tuple:
        """
        解析 100 字节输入位图（25 DWord = 800 位）

        Returns:
            tuple: (changes, bitmap_hex)
                changes: 变化的输入列表 [{gpio: "I0.0", bit: 1}, ...]
                bitmap_hex: 当前完整位图，小写 hex 字符串，100 字节 → 200 字符
                    字节 N 第 K 位对应 I{N}.{K}
        """
        changes = []
        current_bits = {}

        for byte_idx in range(min(100, len(data))):
            byte_val = data[byte_idx]
            if byte_val == 0:
                # 全 0 字节：所有位均为 0
                for bit in range(8):
                    io_addr = f"I{byte_idx}.{bit}"
                    current_bits[io_addr] = 0
            else:
                for bit in range(8):
                    io_addr = f"I{byte_idx}.{bit}"
                    state = 1 if (byte_val & (1 << bit)) else 0
                    current_bits[io_addr] = state

        with self.input_lock:
            for io_addr, state in current_bits.items():
                last = self.plc_input_last.get(io_addr)
                if last is not None and last != state:
                    changes.append({"gpio": io_addr, "bit": state})
                self.plc_input_last[io_addr] = state

        bitmap_hex = data[:100].hex()
        return changes, bitmap_hex

    @staticmethod
    def parse_io_address(io_str):
        """
        解析 PLC IO 地址，支持 Q0.0, I1.2, M10.5, DB11.DBX2.0 等格式

        Args:
            io_str: IO 地址字符串

        Returns:
            tuple: (area, byte, bit, db_number)
                area: snap7 Area 枚举
                byte: 字节地址 (int)
                bit: 位地址 (int)
                db_number: DB 号（非 DB 区域为 0）
        """
        io_str = io_str.strip().upper()

        # DB 地址格式：DB{db_no}.DBX{byte}.{bit}
        match = re.match(r'^DB(\d+)\.DBX(\d+)\.(\d+)$', io_str)
        if match:
            db_number = int(match.group(1))
            byte = int(match.group(2))
            bit = int(match.group(3))
            return IO_AREA_MAP['DB'], byte, bit, db_number

        # 标准地址格式：{area}{byte}.{bit}
        match = re.match(r'^([QIM])(\d+)\.(\d+)$', io_str)
        if not match:
            raise ValueError(
                f"无效的 IO 地址格式: {io_str}，应为 Q0.0, I1.2, M10.0, DB11.DBX2.0 等格式"
            )

        area_type = match.group(1)
        byte = int(match.group(2))
        bit = int(match.group(3))

        if area_type not in IO_AREA_MAP:
            raise ValueError(f"不支持的区域类型: {area_type}，仅支持 Q, I, M, DB")

        return IO_AREA_MAP[area_type], byte, bit, 0

    def connect(self):
        """连接到 S7 PLC"""
        try:
            self.client = Client()
            self.client.connect(self.ip_address, self.rack, self.slot, self.port)
            if self.client.get_connected():
                print(f"成功连接到 PLC: {self.ip_address} (rack={self.rack}, slot={self.slot})")
            else:
                raise ConnectionError(f"无法连接到 PLC: {self.ip_address}")
        except Exception as e:
            print(f"错误: 无法连接到 PLC {self.ip_address}: {e}")
            raise

    def reconnect(self):
        """重新连接 PLC"""
        if self.client:
            try:
                self.client.disconnect()
            except:
                pass
        time.sleep(1)
        self.connect()

    def set_gpio(self, gpio_states):
        """
        设置 PLC 输出状态（优化：同字节 IO 合并为单次写入）

        Args:
            gpio_states: dict {io_address: state, ...}
                io_address: 如 "Q0.0", "Q1.2", "DB11.DBX2.0"
                state: 0 或 1
        """
        with self._lock:
            # 按 area+db_number+byte 分组，合并同字节操作
            byte_ops = {}  # (area, db_number, byte) -> {bit: state, ...}
            for io_addr, state in gpio_states.items():
                io_addr = str(io_addr).strip()
                state = int(state)
                area, byte, bit, db_number = self.parse_io_address(io_addr)
                key = (area, db_number, byte)
                if key not in byte_ops:
                    byte_ops[key] = {}
                byte_ops[key][bit] = state

                # 模拟模式直接更新状态
                if self.simulate:
                    self.gpio_states[io_addr] = state
                    self.current_gpio_states[io_addr] = state
                    if self.debug:
                        print(f"模拟: {io_addr} = {state}")

            if self.simulate:
                return

            # 对每个字节执行单次写入
            for (area, db_number, byte), bit_states in byte_ops.items():
                try:
                    # 读取当前字节
                    data = self.client.read_area(area, db_number, byte, 1)
                    # 修改所有需要改变的位
                    for bit, state in bit_states.items():
                        set_bool(data, 0, bit, bool(state))
                    # 单次写入整个字节
                    self.client.write_area(area, db_number, byte, data)

                    # 更新缓存
                    for bit, state in bit_states.items():
                        if area == Area.DB:
                            addr = f"DB{db_number}.DBX{byte}.{bit}"
                        else:
                            area_prefix = 'Q' if area == Area.PA else 'I' if area == Area.PE else 'M'
                            addr = f"{area_prefix}{byte}.{bit}"
                        self.current_gpio_states[addr] = state
                        self.gpio_states[addr] = state

                    if self.debug:
                        bits_str = ', '.join(f"bit{b}={s}" for b, s in bit_states.items())
                        print(f"已写入字节 {byte} (DB={db_number}): {bits_str} (数据: {data[0]:08b})")

                except Exception as e:
                    print(f"写入 PLC 字节 {byte} 失败: {e}")
                    try:
                        self.reconnect()
                    except:
                        pass

    def read_gpio(self, gpio_pin):
        """
        读取 PLC IO 状态

        Args:
            gpio_pin: IO 地址，如 "Q0.0", "DB11.DBX2.0"

        Returns:
            引脚状态值 (0/1)，失败返回 None
        """
        io_addr = str(gpio_pin).strip()

        if self.simulate:
            return self.gpio_states.get(io_addr, 0)

        with self._lock:
            try:
                area, byte, bit, db_number = self.parse_io_address(io_addr)
                data = self.client.read_area(area, db_number, byte, 1)
                return int(get_bool(data, 0, bit))
            except Exception as e:
                print(f"读取 PLC {io_addr} 失败: {e}")
                try:
                    self.reconnect()
                except:
                    pass
                return None

    def set_spi(self, clk_pin, data_pin, cs_pin, data, cs_collection="down", lag_time=0.001, debug_spi=False):
        """SPI 通信（S7 PLC 暂不支持）"""
        raise NotImplementedError("S7 PLC 暂不支持 SPI 模式")

    def close(self):
        """断开 PLC 连接并停止输入监听"""
        self._running = False
        self.stop_input_listener()
        if self.client:
            try:
                self.client.disconnect()
                print(f"已断开 PLC {self.ip_address} 连接")
            except Exception as e:
                print(f"断开 PLC 连接失败: {e}")


class S7WordReadController(GPIOControllerBase):
    """S7 PLC WORD 读取控制器
    
    用于从西门子 S7 PLC 的 DB 块读取 16 位无符号整数 (WORD)，
    适用于电梯重量等模拟量采集场景。
    """

    def __init__(self, ip_address, port=102, rack=0, slot=1,
                 db_number=1, start_byte=0, simulate=False, debug=False):
        config = {
            'ip_address': ip_address,
            'port': port,
            'rack': rack,
            'slot': slot,
            'db_number': db_number,
            'start_byte': start_byte,
        }
        super().__init__(config, simulate=simulate, debug=debug)

        self.ip_address = ip_address
        self.port = port
        self.rack = rack
        self.slot = slot
        self.db_number = db_number
        self.start_byte = start_byte
        self.client = None

        if not simulate:
            self.connect()
        else:
            print(f"S7 WORD 读取控制器运行在模拟模式，目标: {self.ip_address}")

    def connect(self):
        """连接到 S7 PLC"""
        try:
            self.client = Client()
            self.client.connect(self.ip_address, self.rack, self.slot, self.port)
            if self.client.get_connected():
                print(f"[WORD读取] 成功连接到 PLC: {self.ip_address}")
            else:
                raise ConnectionError(f"无法连接到 PLC: {self.ip_address}")
        except Exception as e:
            print(f"[WORD读取] 错误: 无法连接到 PLC {self.ip_address}: {e}")
            raise

    def reconnect(self):
        """重新连接 PLC"""
        if self.client:
            try:
                self.client.disconnect()
            except:
                pass
        time.sleep(1)
        self.connect()

    def read_word(self, db_number=None, byte=None):
        """
        从 PLC DB 块读取一个 WORD（16 位无符号整数）

        Args:
            db_number: DB 块号，None 使用配置默认值
            byte: 起始字节地址，None 使用配置默认值

        Returns:
            int: 0-65535 的无符号整数值，失败返回 None
        """
        if db_number is None:
            db_number = self.db_number
        if byte is None:
            byte = self.start_byte

        if self.simulate:
            val = random.randint(0, 65535)
            print(f"[WORD读取-模拟] DB{db_number}.DBW{byte} = {val}")
            return val

        try:
            data = self.client.read_area(Area.DB, db_number, byte, 2)
            if len(data) < 2:
                print(f"[WORD读取] 读取 DB{db_number}.DBW{byte} 返回数据不足")
                return None
            value = struct.unpack('>H', data[:2])[0]
            if self.debug:
                print(f"[WORD读取] DB{db_number}.DBW{byte} = {value} (0x{value:04X})")
            return value
        except Exception as e:
            print(f"[WORD读取] 读取 DB{db_number}.DBW{byte} 失败: {e}")
            try:
                self.reconnect()
            except:
                pass
            return None

    def read_words(self, db_number=None, start_byte=None, count=1):
        """
        批量读取多个 WORD

        Args:
            db_number: DB 块号
            start_byte: 起始字节地址
            count: 读取的 WORD 数量

        Returns:
            list: [{byte: addr, word: value, hex: "0xXXXX"}, ...]
                  全部失败返回空列表
        """
        if db_number is None:
            db_number = self.db_number
        if start_byte is None:
            start_byte = self.start_byte

        results = []
        for i in range(count):
            byte_addr = start_byte + i * 2
            val = self.read_word(db_number, byte_addr)
            if val is not None:
                results.append({
                    "byte": byte_addr,
                    "word": val,
                    "hex": f"0x{val:04X}"
                })
        return results

    def set_gpio(self, gpio_states):
        """WORD 读取控制器不支持 GPIO 设置"""
        raise NotImplementedError("S7 WORD 读取控制器不支持 GPIO 设置操作")

    def read_gpio(self, gpio_pin):
        """WORD 读取控制器不支持 GPIO 位读取"""
        raise NotImplementedError("S7 WORD 读取控制器不支持 GPIO 位读取，请使用 read_word")

    def set_spi(self, clk_pin, data_pin, cs_pin, data, cs_collection="down", lag_time=0.001, debug_spi=False):
        """WORD 读取控制器不支持 SPI"""
        raise NotImplementedError("S7 WORD 读取控制器不支持 SPI 操作")

    def close(self):
        """断开 PLC 连接"""
        if self.client:
            try:
                self.client.disconnect()
                print(f"[WORD读取] 已断开 PLC {self.ip_address} 连接")
            except Exception as e:
                print(f"[WORD读取] 断开 PLC 连接失败: {e}")
