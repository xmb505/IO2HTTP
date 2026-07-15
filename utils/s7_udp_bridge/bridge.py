#!/usr/bin/env python3
"""
S7 → UDP Bitmap 万能点位映射桥接器

设计目标:
    在 Linux 上位机侧以 50ms 节拍主动轮询西门子 S7 PLC (snap7 / TCP 102),
    将任意物理点位 (DB/Q/I/M) 投射到 100 字节 UDP Bitmap, 向 daemon_gpio.py
    单向广播。彻底消除 PLC 自身 TUSEND 高频广播对 TIA 协议栈的冲击。

三层映射体系 (合并去重):
    1. bit_mappings   比特粒度批量 (主力, 与字节边界无关, 三 Key 友好写法)
    2. db_mappings    字节粒度批量 (备选; 与 bit_mappings 重叠时后者优先)
    3. fine_mappings  单点位自由 (任意区域; 落点在 1/2 覆盖范围 → 内存直读)

发送策略:
    默认 change-only: 仅当 100 字节 Bitmap 与上次发送不同才 sendto UDP。
    可选 --heartbeat-ms N 在无变化时每 N 毫秒兜底发一次 (保活 NAT/防火墙)。

启动: python3 bridge.py [--config PATH] [--debug] [--heartbeat-ms N]
"""

import argparse
import configparser
import math
import re
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from snap7.client import Client
from snap7.type import Area

# snap7 3.0 把异常类搬到了 snap7.error; 旧版是 snap7.exceptions
try:
    from snap7.exceptions import Snap7Exception  # snap7 < 3.0
except ImportError:
    try:
        from snap7.error import S7Error as Snap7Exception  # snap7 >= 3.0
    except ImportError:
        Snap7Exception = Exception  # 兜底


UDP_BUFFER_SIZE = 100       # 100 字节 = 800 位
UDP_BIT_COUNT = UDP_BUFFER_SIZE * 8
RECONNECT_INTERVAL_S = 1.5  # 后台重连节拍 (秒)
RECONNECT_IDLE_SLEEP_S = 0.3  # client 健康时空闲轮询节拍

# snap7 区域映射 (与 gpio/s7plc_controller.py 对齐)
IO_AREA_MAP = {
    'Q': Area.PA,
    'I': Area.PE,
    'M': Area.MK,
    'DB': Area.DB,
}


# ---------------------------------------------------------------------------
# 点位字符串解析
# ---------------------------------------------------------------------------

_RE_DB_POINT = re.compile(r'^DB(\d+)\.DBX(\d+)\.(\d+)$', re.IGNORECASE)
_RE_STD_POINT = re.compile(r'^([QIM])(\d+)\.(\d+)$', re.IGNORECASE)
_RE_UDP_POINT = re.compile(r'^I(\d+)\.(\d+)$', re.IGNORECASE)


def parse_plc_point(s):
    """
    解析 PLC 物理点位
        DB11.DBX12.4 -> (Area.DB, 11, 12, 4)
        Q0.0         -> (Area.PA, 0, 0, 0)
        I1.2         -> (Area.PE, 0, 1, 2)
        M4.5         -> (Area.MK, 0, 4, 5)
    """
    s = s.strip().upper()
    m = _RE_DB_POINT.match(s)
    if m:
        return Area.DB, int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _RE_STD_POINT.match(s)
    if m:
        prefix = m.group(1)
        return IO_AREA_MAP[prefix], 0, int(m.group(2)), int(m.group(3))
    raise ValueError(f"无法解析 PLC 点位: {s!r} (期望 DB11.DBX0.0 / Q0.0 / I1.2 / M4.5)")


def parse_udp_point(s):
    """解析 UDP 虚拟 I 点 I{byte}.{bit} -> (udp_byte, udp_bit)"""
    s = s.strip().upper()
    m = _RE_UDP_POINT.match(s)
    if not m:
        raise ValueError(f"无法解析 UDP 点位: {s!r} (期望 I10.3 格式)")
    return int(m.group(1)), int(m.group(2))


def format_plc_point(area, db_no, byte, bit):
    if area == Area.DB:
        return f"DB{db_no}.DBX{byte}.{bit}"
    if area == Area.PA:
        return f"Q{byte}.{bit}"
    if area == Area.PE:
        return f"I{byte}.{bit}"
    if area == Area.MK:
        return f"M{byte}.{bit}"
    return f"?{byte}.{bit}"


def _ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class S7UdpBridge:
    def __init__(self, config_path, debug=False, heartbeat_ms=None):
        """
        Args:
            config_path: config.ini 路径
            debug: --debug, 终端打印点跳变
            heartbeat_ms: --heartbeat-ms CLI 覆盖. None=从 config.ini 读; 显式传 int 则覆盖 config.
                          -1 也视为禁用 (与配置 None 行为一致).
        """
        self.config_path = Path(config_path)
        self.debug = debug
        # 心跳节拍优先级: CLI --heartbeat-ms > [heartbeat] interval_ms > 默认 0 (禁用)
        # None 表示「按 config.ini 读取」; 显式 int 表示 CLI 强制覆盖; 0 = 禁用
        self._heartbeat_cli_override = heartbeat_ms
        self.heartbeat_ms = 0  # _load_heartbeat_config 后赋值

        # PLC / UDP / 轮询参数
        self.plc_ip = None
        self.plc_port = 102
        self.plc_rack = 0
        self.plc_slot = 1
        self.udp_dest_ip = '127.0.0.1'
        self.udp_dest_port = 11451
        self.interval_ms = 50

        # 映射数据
        self.db_mappings = []     # [{db_no, src_start, length, udp_start}]
        self.bit_mappings = []    # [{area, db_no, src_byte, src_bit, count, udp_byte, udp_bit}]
        self.fine_cache_hits = [] # cache_source, [{area, db_no, byte, bit, udp_byte, udp_bit}]
        self.fine_external = {}   # (area, db_no, byte) -> [{bit, udp_byte, udp_bit}]
        self.reverse_map = {}     # (udp_byte, udp_bit) -> PLC 点名 (供 debug diff)

        # 运行时状态
        self.client = None
        self.client_lock = threading.Lock()
        self.client_dirty = False

        self.last_good_bitmap = bytearray(UDP_BUFFER_SIZE)  # 断线兜底
        self.last_sent_bitmap = bytearray(UDP_BUFFER_SIZE)   # debug diff 用

        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        cfg = configparser.ConfigParser()
        cfg.read(self.config_path, encoding='utf-8')
        self.cfg = cfg
        self._load_plc_config(cfg)
        self._load_udp_config(cfg)
        self._load_polling_config(cfg)
        self._load_heartbeat_config(cfg)
        self._parse_bit_mappings(cfg)
        self._parse_db_mappings(cfg)
        self._parse_fine_mappings(cfg)
        self._build_reverse_map()

    # ---------- 配置加载 ----------

    def _load_plc_config(self, cfg):
        self.plc_ip = cfg.get('plc', 'ip_address')
        self.plc_port = cfg.getint('plc', 'port', fallback=102)
        self.plc_rack = cfg.getint('plc', 'rack', fallback=0)
        self.plc_slot = cfg.getint('plc', 'slot', fallback=1)

    def _load_udp_config(self, cfg):
        self.udp_dest_ip = cfg.get('udp', 'dest_ip', fallback='127.0.0.1')
        self.udp_dest_port = cfg.getint('udp', 'dest_port', fallback=11451)

    def _load_polling_config(self, cfg):
        self.interval_ms = cfg.getint('polling', 'interval_ms', fallback=50)

    def _load_heartbeat_config(self, cfg):
        """心跳节拍优先级: CLI --heartbeat-ms > [heartbeat] interval_ms > 默认 0 (禁用)."""
        from_config = 0
        if cfg.has_section('heartbeat'):
            from_config = cfg.getint('heartbeat', 'interval_ms', fallback=0)
        if self._heartbeat_cli_override is not None:
            # CLI 强制覆盖 (含 0 = 显式禁用)
            self.heartbeat_ms = max(0, int(self._heartbeat_cli_override))
            self.heartbeat_source = f"CLI --heartbeat-ms={self.heartbeat_ms}"
        else:
            self.heartbeat_ms = max(0, int(from_config))
            self.heartbeat_source = f"[heartbeat] interval_ms={self.heartbeat_ms}"

    # ---------- 映射解析 ----------

    def _parse_bit_mappings(self, cfg):
        if not cfg.has_section('bit_mappings'):
            return

        # configparser 默认会把 key 全部转为小写; 用 dict 直查 + 大小写比对两种方式兼容
        section_items = {k.lower(): (k, v) for k, v in cfg.items('bit_mappings')}

        def _get(option):
            """从 bit_mappings 段里查 option (大小写不敏感)."""
            hit = section_items.get(option.lower())
            return hit[1] if hit else None

        # ----- 人类友好写法: Start_Mapping_I / Start_Mapping_DB / Mapping_Count -----
        i_key = _get('Start_Mapping_I')
        db_key = _get('Start_Mapping_DB')
        cnt_key = _get('Mapping_Count')

        if i_key is not None or db_key is not None or cnt_key is not None:
            missing = [n for n, v in (
                ('Start_Mapping_I', i_key), ('Start_Mapping_DB', db_key), ('Mapping_Count', cnt_key),
            ) if v is None]
            if missing:
                raise ValueError(
                    f"[bit_mappings] 人类友好写法下 3 个 Key 必须齐备, 缺失: {', '.join(missing)}"
                )

            udp_byte, udp_bit = parse_udp_point(i_key)
            src_area, src_db, src_byte, src_bit = parse_plc_point(db_key)
            if src_area != Area.DB:
                raise ValueError(
                    f"[bit_mappings] Start_Mapping_DB={db_key!r} 源点仅支持 DB 区域 (DBn.DBXy.z); "
                    f"Q/I/M 单点位请用 [fine_mappings]"
                )
            count = int(str(cnt_key).strip())

            truncated = self._truncate_bit_count(udp_byte, udp_bit, count, 'bit_mappings[Start_*]')
            if truncated > 0:
                self.bit_mappings.append({
                    'area': src_area,
                    'db_no': src_db,
                    'src_byte': src_byte,
                    'src_bit': src_bit,
                    'count': truncated,
                    'udp_byte': udp_byte,
                    'udp_bit': udp_bit,
                })
            return  # 友好写法独占 [bit_mappings] 段, 不再走老格式

        # ----- 老格式兼容: bit_map_X = 源点, 目的点, count -----
        for key, val in cfg.items('bit_mappings'):
            parts = [p.strip() for p in val.split(',')]
            if len(parts) != 3:
                raise ValueError(f"[bit_mappings] {key}={val!r} 应为 3 段: 源点, 目的点, 位数")
            src_area, src_db, src_byte, src_bit = parse_plc_point(parts[0])
            if src_area != Area.DB:
                raise ValueError(
                    f"[bit_mappings] {key}={val!r} 源点仅支持 DB 区域 (DBn.DBXy.z); "
                    f"Q/I/M 单点位请用 [fine_mappings]"
                )
            udp_byte, udp_bit = parse_udp_point(parts[1])
            count = int(parts[2])

            truncated = self._truncate_bit_count(udp_byte, udp_bit, count, f'bit_mappings.{key}')
            if truncated > 0:
                self.bit_mappings.append({
                    'area': src_area,
                    'db_no': src_db,
                    'src_byte': src_byte,
                    'src_bit': src_bit,
                    'count': truncated,
                    'udp_byte': udp_byte,
                    'udp_bit': udp_bit,
                })

    def _truncate_bit_count(self, udp_byte, udp_bit, count, label):
        """静默截断超界位数. 返回调整后的 count; 完全装不下时返回 0."""
        dst_end = udp_byte * 8 + udp_bit + count
        if dst_end <= UDP_BIT_COUNT:
            return count
        new_count = UDP_BIT_COUNT - udp_byte * 8 - udp_bit
        if new_count <= 0:
            print(f"[{_ts()}] [WARN] [{label}] 完全超出 100 字节边界, 已跳过")
            return 0
        print(f"[{_ts()}] [WARN] [{label}] 超出 100 字节边界, 已静默截断: count={count}->{new_count}")
        return new_count

    def _parse_db_mappings(self, cfg):
        if not cfg.has_section('db_mappings'):
            return
        for key, val in cfg.items('db_mappings'):
            parts = [p.strip() for p in val.split(',')]
            if len(parts) != 4:
                raise ValueError(f"[db_mappings] {key}={val!r} 应为 4 段: DB号, 源起始字节, 长度, UDP起始字节")
            db_no = int(parts[0])
            src_start = int(parts[1])
            length = int(parts[2])
            udp_start = int(parts[3])

            if udp_start + length > UDP_BUFFER_SIZE:
                new_length = UDP_BUFFER_SIZE - udp_start
                if new_length <= 0:
                    print(f"[{_ts()}] [WARN] [db_mappings] {key} 完全超出 100 字节边界, 已跳过")
                    continue
                print(f"[{_ts()}] [WARN] [db_mappings] {key} 超出 100 字节边界, 已静默截断 length={length}->{new_length}")
                length = new_length

            self.db_mappings.append({
                'db_no': db_no,
                'src_start': src_start,
                'length': length,
                'udp_start': udp_start,
            })

    def _parse_fine_mappings(self, cfg):
        # 1) 收集所有"批量覆盖区间": (area, db_no, byte_start, byte_end_inclusive)
        covered = []  # list of (area, db_no, byte_lo, byte_hi_exclusive)
        for m in self.db_mappings:
            covered.append((Area.DB, m['db_no'], m['src_start'], m['src_start'] + m['length']))
        for m in self.bit_mappings:
            src_byte_len = math.ceil((m['src_bit'] + m['count']) / 8)
            covered.append((m['area'], m['db_no'], m['src_byte'], m['src_byte'] + src_byte_len))

        def is_covered(area, db_no, byte):
            for (a, d, lo, hi) in covered:
                if a == area and d == db_no and lo <= byte < hi:
                    return True
            return False

        # 2) 解析 fine_mappings
        if not cfg.has_section('fine_mappings'):
            return
        for key, val in cfg.items('fine_mappings'):
            src_area, src_db, src_byte, src_bit = parse_plc_point(key)
            udp_byte, udp_bit = parse_udp_point(val)
            entry = {
                'area': src_area,
                'db_no': src_db,
                'byte': src_byte,
                'bit': src_bit,
                'udp_byte': udp_byte,
                'udp_bit': udp_bit,
            }
            if is_covered(src_area, src_db, src_byte):
                self.fine_cache_hits.append(entry)
            else:
                self.fine_external.setdefault(
                    (src_area, src_db, src_byte), []
                ).append({'bit': src_bit, 'udp_byte': udp_byte, 'udp_bit': udp_bit})

    def _build_reverse_map(self):
        """(udp_byte, udp_bit) -> PLC 点名字符串, 用于 --debug 差分日志. 后写者优先."""
        # db_mappings: 整字节块 → 8 位全部填满
        for m in self.db_mappings:
            for i in range(m['length'] * 8):
                ub = m['udp_start'] + i // 8
                ubt = i % 8
                sb = m['src_start'] + i // 8
                sbt = i % 8
                self.reverse_map[(ub, ubt)] = f"DB{m['db_no']}.DBX{sb}.{sbt}"
        # bit_mappings: 逐位
        for m in self.bit_mappings:
            for i in range(m['count']):
                sbi = m['src_bit'] + i
                ubi = m['udp_bit'] + i
                sb = m['src_byte'] + sbi // 8
                sbt = sbi % 8
                ub = m['udp_byte'] + ubi // 8
                ubt = ubi % 8
                # 修正: ubi 应当从 udp_byte*8 起步, 与 _read_cycle 对齐
                dst_lin = m['udp_byte'] * 8 + m['udp_bit'] + i
                ub = dst_lin // 8
                ubt = dst_lin % 8
                self.reverse_map[(ub, ubt)] = f"DB{m['db_no']}.DBX{sb}.{sbt}"
        # fine_mappings: 单点位 (后写者优先, 覆盖上面的)
        for f in self.fine_cache_hits:
            self.reverse_map[(f['udp_byte'], f['udp_bit'])] = format_plc_point(
                f['area'], f['db_no'], f['byte'], f['bit']
            )
        for (area, db_no, byte), entries in self.fine_external.items():
            for e in entries:
                self.reverse_map[(e['udp_byte'], e['udp_bit'])] = format_plc_point(
                    area, db_no, byte, e['bit']
                )

    # ---------- S7 连接 ----------

    def _connect_plc(self):
        """同步连接 PLC. 成功返回 Client; 失败抛异常."""
        c = Client()
        c.connect(self.plc_ip, self.plc_rack, self.plc_slot, self.plc_port)
        if not c.get_connected():
            try:
                c.disconnect()
            except Exception:
                pass
            raise ConnectionError(f"无法连接 PLC: {self.plc_ip}")
        return c

    def _reconnect_loop(self):
        """后台重连线程: client_dirty=True 时每 1.5s 尝试重建连接."""
        while True:
            if not self.client_dirty:
                time.sleep(RECONNECT_IDLE_SLEEP_S)
                continue
            try:
                new_client = self._connect_plc()
                with self.client_lock:
                    old = self.client
                    self.client = new_client
                    self.client_dirty = False
                if old is not None:
                    try:
                        old.disconnect()
                    except Exception:
                        pass
                print(f"[{_ts()}] [RECONNECT] PLC 重连成功")
            except Exception as e:
                print(f"[{_ts()}] [RECONNECT] 重连失败: {e} ({RECONNECT_INTERVAL_S}s 后重试)")
                time.sleep(RECONNECT_INTERVAL_S)

    # ---------- 单轮读取 ----------

    def _read_cycle(self, client, tmp_buffer, byte_cache):
        """
        一轮读取: 填充 tmp_buffer 与 byte_cache.
        抛出 Snap7Exception / OSError 时由 main loop 兜底.
        """
        # 1) db_mappings: 字节批量
        for m in self.db_mappings:
            data = client.read_area(Area.DB, m['db_no'], m['src_start'], m['length'])
            tmp_buffer[m['udp_start']:m['udp_start'] + m['length']] = data
            for i in range(m['length']):
                byte_cache[(Area.DB, m['db_no'], m['src_start'] + i)] = data[i]

        # 2) bit_mappings: 比特批量 (覆盖 db_mappings 的 UDP 同区位)
        for m in self.bit_mappings:
            src_byte_len = math.ceil((m['src_bit'] + m['count']) / 8)
            if src_byte_len <= 0:
                continue
            data = client.read_area(m['area'], m['db_no'], m['src_byte'], src_byte_len)
            for i in range(src_byte_len):
                byte_cache[(m['area'], m['db_no'], m['src_byte'] + i)] = data[i]
            # 位粒度写入 (后写覆盖先写)
            for i in range(m['count']):
                src_bit_idx = m['src_bit'] + i
                dst_bit_idx = m['udp_byte'] * 8 + m['udp_bit'] + i
                sb = src_bit_idx // 8
                sbt = src_bit_idx % 8
                db_ = dst_bit_idx // 8
                dbt = dst_bit_idx % 8
                if data[sb] & (1 << sbt):
                    tmp_buffer[db_] |= (1 << dbt)
                else:
                    tmp_buffer[db_] &= ~(1 << dbt)

        # 3) fine_mappings external: 按 (Area, db_no, byte) 去重单字节读
        for (area, db_no, byte), entries in self.fine_external.items():
            key = (area, db_no, byte)
            if key in byte_cache:
                byte_val = byte_cache[key]
            else:
                data = client.read_area(area, db_no, byte, 1)
                byte_val = data[0]
                byte_cache[key] = byte_val
            for e in entries:
                if byte_val & (1 << e['bit']):
                    tmp_buffer[e['udp_byte']] |= (1 << e['udp_bit'])
                else:
                    tmp_buffer[e['udp_byte']] &= ~(1 << e['udp_bit'])

        # 4) fine_mappings cache_source: 纯内存, 不发 PLC 请求
        for f in self.fine_cache_hits:
            key = (f['area'], f['db_no'], f['byte'])
            if key not in byte_cache:
                # 覆盖登记时已声明 cache_source, 必有缓存; 兜底跳过
                continue
            byte_val = byte_cache[key]
            if byte_val & (1 << f['bit']):
                tmp_buffer[f['udp_byte']] |= (1 << f['udp_bit'])
            else:
                tmp_buffer[f['udp_byte']] &= ~(1 << f['udp_bit'])

    # ---------- debug 差分日志 ----------

    def _print_diff(self, new_buffer):
        changed = []
        for byte_idx in range(UDP_BUFFER_SIZE):
            old = self.last_sent_bitmap[byte_idx]
            new = new_buffer[byte_idx]
            if old == new:
                continue
            diff = old ^ new
            for bit in range(8):
                if diff & (1 << bit):
                    src_label = self.reverse_map.get((byte_idx, bit), f"?{byte_idx}.{bit}")
                    old_v = (old >> bit) & 1
                    new_v = (new >> bit) & 1
                    changed.append((byte_idx, bit, src_label, old_v, new_v))

        if not changed:
            return

        ts = _ts()
        for byte_idx, bit, src_label, old_v, new_v in changed:
            udp_label = f"I{byte_idx}.{bit}"
            print(f"[DEBUG {ts}] 点位跳变 -> {src_label} (对应虚拟{udp_label}) : {old_v} -> {new_v}")

    # ---------- 启动概要 ----------

    def _print_startup_summary(self):
        print(f"[{_ts()}] [CONFIG] PLC: {self.plc_ip}:{self.plc_port} rack={self.plc_rack} slot={self.plc_slot}")
        print(f"[{_ts()}] [CONFIG] UDP: {self.udp_dest_ip}:{self.udp_dest_port}, 间隔 {self.interval_ms}ms")
        print(f"[{_ts()}] [CONFIG] bit_mappings: {len(self.bit_mappings)} 条")
        for i, m in enumerate(self.bit_mappings, 1):
            # 自动算出末位, 让现场一眼能对齐
            last_src_lin = m['src_bit'] + m['count'] - 1
            last_dst_lin = m['udp_byte'] * 8 + m['udp_bit'] + m['count'] - 1
            print(
                f"           #{i}: DB{m['db_no']}.DBX{m['src_byte']}.{m['src_bit']} 起 {m['count']} 位 "
                f"-> I{m['udp_byte']}.{m['udp_bit']} 起"
            )
            print(
                f"              末位: 源 DB{m['db_no']}.DBX{m['src_byte'] + last_src_lin // 8}.{last_src_lin % 8} "
                f"-> UDP I{last_dst_lin // 8}.{last_dst_lin % 8}"
            )
        print(f"[{_ts()}] [CONFIG] db_mappings: {len(self.db_mappings)} 条")
        for i, m in enumerate(self.db_mappings, 1):
            print(f"           #{i}: DB{m['db_no']}.DBB{m['src_start']}..{m['src_start']+m['length']-1} ({m['length']}字节) -> UDP[{m['udp_start']}..{m['udp_start']+m['length']-1}]")
        print(f"[{_ts()}] [CONFIG] fine_mappings: cache_source={len(self.fine_cache_hits)}, external={sum(len(v) for v in self.fine_external.values())}")

    # ---------- 单轮执行 (供主循环调用, 也供测试独立验证) ----------

    def _run_one_cycle(self):
        """一周期: 读 PLC → 写 tmp_buffer → 变化时/心跳时 sendto → debug 差分打印"""
        self.cycle_start_time = time.monotonic()
        if not hasattr(self, 'last_heartbeat_time'):
            self.last_heartbeat_time = self.cycle_start_time

        tmp_buffer = bytearray(UDP_BUFFER_SIZE)
        byte_cache = {}

        # 1) 读取
        try:
            with self.client_lock:
                if self.client is None or self.client_dirty:
                    raise ConnectionError("PLC 客户端不可用, 使用兜底")
                self._read_cycle(self.client, tmp_buffer, byte_cache)
            self.last_good_bitmap[:] = tmp_buffer
        except (Snap7Exception, OSError, ConnectionError) as e:
            print(f"\033[33m[{_ts()}] [WARN] S7 通信异常: {e}; 沿用 last_good_bitmap 兜底\033[0m")
            tmp_buffer[:] = self.last_good_bitmap
            with self.client_lock:
                if self.client is not None and not self.client_dirty:
                    self.client_dirty = True

        # 2) 发送决策 (change-only + 可选 heartbeat)
        bitmap_bytes = bytes(tmp_buffer)
        changed = bitmap_bytes != bytes(self.last_sent_bitmap)
        now = time.monotonic()

        if changed:
            self.udp_socket.sendto(bitmap_bytes, (self.udp_dest_ip, self.udp_dest_port))
            self.last_sent_bitmap[:] = tmp_buffer
            self.last_heartbeat_time = now  # 刚发过, 心跳无需立刻再补
        elif self.heartbeat_ms > 0 and (now - self.last_heartbeat_time) * 1000.0 >= self.heartbeat_ms:
            self.udp_socket.sendto(bitmap_bytes, (self.udp_dest_ip, self.udp_dest_port))
            self.last_sent_bitmap[:] = tmp_buffer
            self.last_heartbeat_time = now
            if self.debug:
                print(f"[DEBUG {_ts()}] 心跳包发送 (无变化, 兜底节拍 {self.heartbeat_ms}ms)")

        # 3) debug 差分打印 (按变化打, 与是否发 UDP 无关)
        if self.debug and changed:
            self._print_diff(tmp_buffer)

    # ---------- 主循环 ----------

    def run_forever(self):
        self._print_startup_summary()

        # 初始连接 (前台, 失败立即退出)
        try:
            with self.client_lock:
                self.client = self._connect_plc()
                self.client_dirty = False
            print(f"[{_ts()}] [CONNECT] 已连接 PLC {self.plc_ip}")
        except Exception as e:
            print(f"[{_ts()}] [FATAL] 初始连接失败: {e}")
            return

        # 后台重连线程
        t = threading.Thread(target=self._reconnect_loop, daemon=True, name="PLC-Reconnector")
        t.start()

        if self.heartbeat_ms > 0:
            send_mode = f"change-only + heartbeat {self.heartbeat_ms}ms"
        else:
            send_mode = "change-only (无心跳)"
        print(f"[{_ts()}] [RUN] 轮询开始, 节拍 {self.interval_ms}ms, 发送策略: {send_mode} (来源: {self.heartbeat_source})")

        try:
            while True:
                self._run_one_cycle()  # 心跳计时状态保存在 self.last_heartbeat_time

                # 节拍
                elapsed = time.monotonic() - self.cycle_start_time
                sleep_s = self.interval_ms / 1000.0 - elapsed
                if sleep_s > 0:
                    time.sleep(sleep_s)
        except KeyboardInterrupt:
            print(f"\n[{_ts()}] [STOP] 用户中断")
        finally:
            try:
                self.udp_socket.close()
            except Exception:
                pass
            with self.client_lock:
                if self.client is not None:
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='S7 → UDP Bitmap 桥接器 (50ms 轮询, change-only 发送, 防抖 last_good_bitmap, 后台重连)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python3 bridge.py                              # 按 config.ini 静默运行, change-only
  python3 bridge.py --debug                      # 开启差分日志, 终端看哪个 I 变了
  python3 bridge.py --heartbeat-ms 1000          # 1000ms 兜底心跳保活 UDP 流
  python3 bridge.py --config /path/cfg.ini       # 指定自定义配置
        """,
    )
    parser.add_argument(
        '--config',
        default=str(Path(__file__).parent / 'config.ini'),
        help='配置文件路径 (默认: 与 bridge.py 同目录的 config.ini)',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='开启差分日志: 仅在 UDP Bitmap 发生变化时, 终端打印具体跳变点位 (源 PLC 点 + 虚拟 I 点 + old->new)',
    )
    parser.add_argument(
        '--heartbeat-ms',
        type=int,
        default=None,
        metavar='N',
        help='心跳节拍覆盖 (毫秒). 不传 = 按 config.ini 的 [heartbeat] interval_ms; '
             '传 0 = 显式禁用; 传 N > 0 = 强制覆盖为 N 毫秒. '
             '设 ≤ interval_ms 等于每轮都发; 较大值仅在无变化时按 N 毫秒兜底发一次 (NAT/防火墙保活)',
    )
    args = parser.parse_args()

    try:
        bridge = S7UdpBridge(args.config, debug=args.debug, heartbeat_ms=args.heartbeat_ms)
    except Exception as e:
        print(f"[{_ts()}] [FATAL] 配置加载失败: {e}", file=sys.stderr)
        sys.exit(2)

    bridge.run_forever()


if __name__ == '__main__':
    main()