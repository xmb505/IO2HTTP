#!/usr/bin/env python3
"""
GPIO 控制器基类
定义所有通信协议控制器（USB2GPIO、S7 PLC 等）的统一接口
"""


class GPIOControllerBase:
    """GPIO 控制器抽象基类"""

    def __init__(self, config, simulate=False, debug=False):
        """
        初始化控制器

        Args:
            config: 协议特定配置字典
            simulate: 是否使用模拟模式
            debug: 是否启用调试输出
        """
        self.simulate = simulate
        self.debug = debug
        self.config = config
        self.gpio_states = {}
        self.current_gpio_states = {}
        self.data_buffer = ""

    def connect(self):
        """连接到硬件设备，子类必须实现"""
        raise NotImplementedError

    def reconnect(self):
        """重新连接设备，子类必须实现"""
        raise NotImplementedError

    def set_gpio(self, gpio_states):
        """
        设置 GPIO 状态

        Args:
            gpio_states: dict {pin: state, ...}
        """
        raise NotImplementedError

    def read_gpio(self, gpio_pin):
        """
        读取 GPIO 状态

        Args:
            gpio_pin: GPIO 引脚编号

        Returns:
            引脚状态值 (0/1)，失败返回 None
        """
        raise NotImplementedError

    def read_word(self, db_number=None, byte=None):
        """
        读取 WORD 值（16位无符号整数），子类可选实现

        Args:
            db_number: DB 块号
            byte: 起始字节地址

        Returns:
            16 位无符号整数值，失败返回 None
        """
        raise NotImplementedError

    def set_spi(self, clk_pin, data_pin, cs_pin, data, cs_collection="down", lag_time=0.001, debug_spi=False):
        """
        SPI 通信（如果协议支持）

        Args:
            clk_pin: 时钟引脚
            data_pin: 数据引脚
            cs_pin: 片选引脚
            data: SPI 数据字符串 (如 "10000100")
            cs_collection: 触发沿 "down" 或 "up"
            lag_time: 操作间隔时间（秒）
            debug_spi: 是否输出 SPI 调试信息
        """
        raise NotImplementedError

    def close(self):
        """关闭连接，子类必须实现"""
        raise NotImplementedError
