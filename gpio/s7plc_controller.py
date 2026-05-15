#!/usr/bin/env python3
"""
S7 PLC 控制器模块
通过 snap7 库与西门子 S7 PLC 进行通信
"""

import re
import time
import threading

from snap7.client import Client
from snap7.type import Area
from snap7.util import set_bool, get_bool

from .base_controller import GPIOControllerBase

# PLC IO 地址到 snap7 Area 的映射
IO_AREA_MAP = {
    'Q': Area.PA,   # 输出过程映像
    'I': Area.PE,   # 输入过程映像
    'M': Area.MK,   # 存储器区
}


class S7PLCController(GPIOControllerBase):
    """S7 PLC 控制器，通过 snap7 库与西门子 S7 PLC 通信"""

    def __init__(self, ip_address, port=102, rack=0, slot=1, simulate=False, debug=False):
        config = {
            'ip_address': ip_address,
            'port': port,
            'rack': rack,
            'slot': slot,
        }
        super().__init__(config, simulate=simulate, debug=debug)

        self.ip_address = ip_address
        self.port = port
        self.rack = rack
        self.slot = slot
        self.client = None
        self._lock = threading.Lock()  # PLC 操作锁

        if not simulate:
            self.connect()
        else:
            print(f"S7 PLC 控制器运行在模拟模式，目标: {self.ip_address}")

    @staticmethod
    def parse_io_address(io_str):
        """
        解析 PLC IO 地址，支持 Q0.0, I1.2, M10.5 等格式

        Args:
            io_str: IO 地址字符串

        Returns:
            tuple: (area, byte, bit)
                area: snap7 Area 枚举
                byte: 字节地址 (int)
                bit: 位地址 (int)
        """
        io_str = io_str.strip().upper()
        match = re.match(r'^([QIM])(\d+)\.(\d+)$', io_str)
        if not match:
            raise ValueError(f"无效的 IO 地址格式: {io_str}，应为 Q0.0, I1.2, M10.0 等格式")

        area_type = match.group(1)
        byte = int(match.group(2))
        bit = int(match.group(3))

        if area_type not in IO_AREA_MAP:
            raise ValueError(f"不支持的区域类型: {area_type}，仅支持 Q, I, M")

        return IO_AREA_MAP[area_type], byte, bit

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
                io_address: 如 "Q0.0", "Q1.2"
                state: 0 或 1
        """
        with self._lock:
            # 按 area+byte 分组，合并同字节操作
            byte_ops = {}  # (area, byte) -> {bit: state, ...}
            for io_addr, state in gpio_states.items():
                io_addr = str(io_addr).strip()
                state = int(state)
                area, byte, bit = self.parse_io_address(io_addr)
                key = (area, byte)
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
            for (area, byte), bit_states in byte_ops.items():
                try:
                    # 读取当前字节
                    data = self.client.read_area(area, 0, byte, 1)
                    # 修改所有需要改变的位
                    for bit, state in bit_states.items():
                        set_bool(data, 0, bit, bool(state))
                    # 单次写入整个字节
                    self.client.write_area(area, 0, byte, data)

                    # 更新缓存
                    for bit, state in bit_states.items():
                        addr = f"{'Q' if area == Area.PA else 'I' if area == Area.PE else 'M'}{byte}.{bit}"
                        self.current_gpio_states[addr] = state
                        self.gpio_states[addr] = state

                    if self.debug:
                        bits_str = ', '.join(f"bit{b}={s}" for b, s in bit_states.items())
                        print(f"已写入字节 {byte}: {bits_str} (数据: {data[0]:08b})")

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
            gpio_pin: IO 地址，如 "Q0.0"

        Returns:
            引脚状态值 (0/1)，失败返回 None
        """
        io_addr = str(gpio_pin).strip()

        if self.simulate:
            return self.gpio_states.get(io_addr, 0)

        with self._lock:
            try:
                area, byte, bit = self.parse_io_address(io_addr)
                data = self.client.read_area(area, 0, byte, 1)
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
        """断开 PLC 连接"""
        if self.client:
            try:
                self.client.disconnect()
                print(f"已断开 PLC {self.ip_address} 连接")
            except Exception as e:
                print(f"断开 PLC 连接失败: {e}")
