#!/usr/bin/env python3
"""
S7 PLC Test Utility
用于通过 snap7 库向西门子 S7 PLC 发送控制命令

Usage:
    ./s7_test.py --ip 192.168.1.8 --io q0.0 --value 1
    ./s7_test.py --ip 192.168.1.8 --io I0.1 --value 0  (读取输入，仅作演示)

支持的 IO 类型:
    Q - 输出 (Q0.0, Q0.1, Q1.0, etc.)
    M - 存储器 (M0.0, M10.0, etc.)
"""

import argparse
import sys
import os
import re

# 优先使用项目本地的 snap7 库
_local_snap7_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'snap7_lib')
if os.path.exists(_local_snap7_path):
    sys.path.insert(0, _local_snap7_path)

try:
    import snap7
    from snap7.util import set_bool, get_bool
    from snap7 import Area  # 新版本 snap7 使用 Area 而不是 Areas
except ImportError:
    print("错误: 未找到 snap7 库")
    print("请安装: pip install python-snap7")
    print("或 clone 到项目下: git clone https://github.com/gijzelaerr/python-snap7.git debug_utils/snap7_lib")
    print("同时需要安装 snap7 系统库: sudo apt install libsnap7-1")
    sys.exit(1)


def parse_io_address(io_str):
    """
    解析 IO 地址字符串，如 'q0.0', 'Q1.2', 'm10.0'

    Returns:
        tuple: (area, byte, bit)
            area: Areas.PA (输出Q), Areas.MK (存储器M), Areas.PE (输入I)
            byte: 字节地址 (int)
            bit: 位地址 (int)
    """
    io_str = io_str.strip().upper()

    # 匹配格式: Q0.0, I1.2, M10.5
    pattern = r'^([QIM])(\d+)\.(\d+)$'
    match = re.match(pattern, io_str)

    if not match:
        raise ValueError(f"无效的 IO 地址格式: {io_str}，应为 Q0.0, I1.2, M10.0 等格式")

    area_type = match.group(1)
    byte = int(match.group(2))
    bit = int(match.group(3))

    # 映射到 snap7 的 Area
    area_map = {
        'Q': Area.PA,   # 输出过程映像
        'I': Area.PE,   # 输入过程映像
        'M': Area.MK,   # 存储器区
    }

    if area_type not in area_map:
        raise ValueError(f"不支持的区域类型: {area_type}，仅支持 Q, I, M")

    return area_map[area_type], byte, bit


def write_to_plc(ip, io_str, value, port=102, rack=0, slot=1):
    """
    向 PLC 写入数据

    Args:
        ip: PLC IP 地址
        io_str: IO 地址，如 'Q0.0'
        value: 值 (0 或 1)
        port: S7 端口 (默认 102)
        rack: 机架号 (默认 0)
        slot: 槽号 (默认 1，S7-1200/1500 通常为 1)
    """
    client = snap7.client.Client()

    try:
        print(f"正在连接 PLC: {ip}:{port} (rack={rack}, slot={slot})")
        client.connect(ip, rack, slot, port)

        if client.get_connected():
            print("连接成功!")
        else:
            print("错误: 连接失败")
            sys.exit(1)

        # 解析 IO 地址
        area, byte, bit = parse_io_address(io_str)
        area_name = 'Q' if area == Area.PA else ('I' if area == Area.PE else 'M')

        print(f"目标: {area_name}{byte}.{bit} = {value}")

        # 读取当前字节
        data = client.read_area(area, 0, byte, 1)
        print(f"当前字节值: {data[0]:08b} (binary)")

        # 设置位值
        set_bool(data, 0, bit, bool(value))
        print(f"新字节值: {data[0]:08b} (binary)")

        # 写入 PLC
        client.write_area(area, 0, byte, data)
        print(f"成功! {area_name}{byte}.{bit} 已设置为 {value}")

    except snap7.exceptions.Snap7Exception as e:
        print(f"PLC 通信错误: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)
    finally:
        client.disconnect()
        print("连接已断开")


def main():
    parser = argparse.ArgumentParser(
        description='S7 PLC 测试工具 - 通过 HTTP 方式控制 PLC IO',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --ip 192.168.1.8 --io q0.0 --value 1    # 将 Q0.0 设置为 1
  %(prog)s --ip 192.168.1.8 --io Q0.0 --value 0    # 将 Q0.0 设置为 0
  %(prog)s --ip 192.168.1.8 --io m10.0 --value 1   # 将 M10.0 设置为 1
        """
    )

    parser.add_argument('--ip', required=True, help='PLC IP 地址 (例如: 192.168.1.8)')
    parser.add_argument('--io', required=True, help='IO 地址 (例如: q0.0, Q1.2, m10.0)')
    parser.add_argument('--value', type=int, required=True, choices=[0, 1],
                        help='值 (0 或 1)')
    parser.add_argument('--port', type=int, default=102, help='S7 端口 (默认: 102)')
    parser.add_argument('--rack', type=int, default=0, help='机架号 (默认: 0)')
    parser.add_argument('--slot', type=int, default=1, help='槽号 (默认: 1)')

    args = parser.parse_args()

    write_to_plc(
        ip=args.ip,
        io_str=args.io,
        value=args.value,
        port=args.port,
        rack=args.rack,
        slot=args.slot
    )


if __name__ == '__main__':
    main()
