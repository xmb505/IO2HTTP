# IO2HTTP

将 USB2GPIO 设备和西门子 S7 PLC 抽象为 Unix Socket、HTTP、WebSocket 接口的 GPIO 守护进程系统，支持远程控制与事件驱动实时监控。

## 功能特性

- **多协议支持**：USB2GPIO（串口通信）、S7 PLC（西门子 S7 协议），可通过工厂方法快速扩展
- **多模式操作**：seter（输出控制）、geter（输入读取）、SPI（bit-banging 通信）
- **多接口**：Unix Socket（控制+状态）、HTTP REST API、WebSocket 实时推送
- **批量控制**：支持单路/多路 GPIO 同时操作；S7 PLC 同字节 IO 合并为单次写入
- **SPI 通信**：基于 bit-banging 的低速 SPI，最多 14 路独立片选
- **事件驱动**：USB2GPIO 轮询上报 + PLC 主动 TCP 位图推送，变化广播到 WebSocket/Unix Socket
- **主从架构**：通过 HTTP 接口可在一台机器远程控制另一台机器
- **模拟模式**：无硬件时通过 `--simulate` 参数进行开发和测试

## 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                     IO2HTTP Daemon                            │
│                                                               │
│  ┌──────────────┐  ┌──────────┐  ┌────────────────┐         │
│  │  Unix Socket  │  │   HTTP   │  │   WebSocket    │         │
│  │  (控制/状态)   │  │   REST   │  │   (状态推送)    │         │
│  └──────┬───────┘  └────┬─────┘  └───────┬────────┘         │
│         │               │                │                    │
│  ┌──────┴───────────────┴────────────────┴───────────────┐   │
│  │                  GPIO 控制核心                          │   │
│  │        命令解析 / 状态缓存 / SPI 队列管理                 │   │
│  │        协议工厂 (USB2GPIO / S7 PLC)                     │   │
│  └────┬───────────────────────────────┬──────────────────┘   │
│       │                               │                       │
│  ┌────┴──────────────┐       ┌───────┴───────────────┐       │
│  │  USB2GPIO Controller│       │  S7 PLC Controller    │       │
│  │  串口通信 / 指令编码  │       │   S7 协议 / TCP 监听   │       │
│  └────────┬───────────┘       └───────┬───────────────┘       │
└───────────┼───────────────────────────┼──────────────────────┘
            │                           │
   ┌────────┴────────┐         ┌────────┴────────┐
   │  USB2GPIO 设备    │         │  Siemens S7 PLC  │
   │  (BL-ENV-V1.3)   │         │  (Q/I/M IO)      │
   └─────────────────┘         └─────────────────┘
```

## 文档目录

| 文档 | 内容 |
|------|------|
| [命令格式](docs/command_format.md) | 单路/批量 GPIO 及 SPI 命令 JSON 格式 |
| [HTTP API](docs/http_api.md) | REST 接口、curl/Python/JavaScript 示例、错误码、主从架构 |
| [WebSocket 事件监听](docs/websocket.md) | 事件驱动实时推送、JS/Python/Node.js 示例、断线重连 |
| [状态格式与交互协议](docs/status_format.md) | GPIO 状态消息格式、ACK 确认、Unix Socket 查询/响应协议 |
| [实现原理](docs/implementation.md) | 系统架构和通信机制设计 |

## 快速开始

### 依赖

- Python 3
- `pyserial` — 串口通信
- `python-snap7` — S7 PLC 通信（S7 协议时需要）
- `websocket-client` — WebSocket 客户端（可选）

```bash
# 创建虚拟环境
python3 -m venv .venv

# 安装依赖（Ubuntu / Fedora）
.venv/bin/pip install -r requirements.txt
```

### 启动

```bash
python3 daemon_gpio.py --simulate    # 模拟模式（无需硬件）
python3 daemon_gpio.py               # 生产模式
python3 daemon_gpio.py --debug       # 调试模式

./start_daemon.sh                    # 使用启动脚本
```

## 配置说明

### daemon_config

```ini
[daemon_config]
socket_path = /tmp/gpio.sock        # 控制命令 Unix Socket
get_statu_path = /tmp/gpio_get.sock # 状态监听 Unix Socket
http_port = 8080                    # HTTP 服务端口（0 = 禁用）
ws_port = 8081                      # WebSocket 端口（0 = 禁用）
```

### USB2GPIO 设备

| 参数 | 说明 | 示例 |
|------|------|------|
| `protocol` | `usb2gpio` | `usb2gpio` |
| `tty_path` | 串口设备路径 | `/dev/ttyUSB0` |
| `baudrate` | 波特率 | `115200` |
| `alias` | 设备别名 | `sender` |
| `mode` | `seter` / `geter` / `spi` | `seter` |

### S7 PLC 设备

| 参数 | 说明 | 示例 |
|------|------|------|
| `protocol` | `s7` | `s7` |
| `ip_address` | PLC IP 地址 | `192.168.1.8` |
| `port` | S7 端口（默认 102） | `102` |
| `rack` | 机架号 | `0` |
| `slot` | 槽号（S7-1200/1500 通常为 1） | `1` |
| `read_port` | 主动输入上报 TCP 监听端口（0 = 禁用） | `11451` |
| `alias` | 设备别名 | `plc` |

> S7 PLC 是双工设备，配置中无需 `mode`，由 JSON 命令的 `mode` 字段决定读写。

### 工作模式

| 模式 | 用途 | 适用协议 |
|------|------|----------|
| `seter` | 输出控制（继电器、门锁等） | USB2GPIO / S7 PLC |
| `geter` | 输入读取（传感器、按钮等） | USB2GPIO / S7 PLC |
| `spi` | bit-banging SPI 通信 | USB2GPIO |

### S7 PLC IO 地址

| 类型 | Snap7 Area | 说明 | 示例 |
|------|-----------|------|------|
| Q | PA（输出过程映像） | 控制继电器、指示灯等 | `Q0.0`, `Q1.2` |
| I | PE（输入过程映像） | 读取传感器、按钮等 | `I0.0`, `I3.5` |
| M | MK（存储器区） | 中间状态标志 | `M10.0`, `M20.3` |

### PLC 主动输入上报

PLC 通过 TCP 将 100 字节（800 位，I0.0–I99.7）输入位图推送到 `read_port`，守护进程解析并与上一帧对比，仅广播变化的输入位 → 见 [WebSocket 事件监听](docs/websocket.md)。

## 项目结构

```
├── daemon_gpio.py              # 入口脚本
├── config/
│   └── config.ini              # 配置文件
├── gpio/
│   ├── base_controller.py      # 控制器基类（统一接口）
│   ├── controller.py           # USB2GPIO 控制器（串口通信）
│   ├── s7plc_controller.py     # S7 PLC 控制器（S7 协议 + TCP 监听）
│   ├── daemon.py               # 守护进程核心逻辑
│   ├── server_http.py          # HTTP 服务
│   └── server_ws.py            # WebSocket 服务
├── docs/
│   ├── command_format.md       # 命令格式说明
│   ├── http_api.md             # HTTP API 文档
│   ├── websocket.md            # WebSocket 事件驱动监听文档
│   ├── implementation.md       # 实现原理
│   └── status_format.md        # 状态格式与交互协议
├── debug_utils/
│   ├── s7_test.py              # S7 PLC 通信测试
│   ├── get_plc_input.py        # PLC 输入上报模拟程序
│   └── snap7_lib/              # Snap7 库
├── test_daemon.py              # 功能测试
├── test_fix.py                 # 集成测试
├── start_daemon.sh             # 启动脚本
└── stop_daemon.sh              # 停止脚本
```

## 开发路线图

- [x] Unix Socket 控制与状态监听
- [x] HTTP REST API
- [x] WebSocket 实时推送
- [x] 批量 GPIO 控制
- [x] Bit-banging SPI 通信
- [x] 主从架构支持
- [x] 西门子 S7 PLC 支持（基本读写）
- [x] S7 PLC 主动输入上报（TCP 位图监听 + 变化广播）

## 许可证

[MIT](LICENSE)

Copyright (c) 2026 新毛宝贝
