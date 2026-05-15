# IO2HTTP

将 USB2GPIO 设备抽象为 Unix Socket、HTTP、WebSocket 接口的 GPIO 守护进程系统，支持远程 GPIO 控制与实时状态监控。

## 功能特性

- **多模式操作**：支持 seter（输出控制）、geter（输入读取）、SPI（bit-banging 通信）三种模式
- **多协议接口**：Unix Socket、HTTP REST API、WebSocket 实时推送
- **批量控制**：支持单路/多路 GPIO 同时操作
- **SPI 通信**：基于 bit-banging 的低速 SPI，最多 14 路独立片选
- **实时监控**：GPIO 状态变化主动推送，支持 ACK 确认机制
- **主从架构**：通过 HTTP 接口可在一台机器控制另一台机器的 GPIO
- **模拟模式**：无硬件时可通过 `--simulate` 参数进行开发和测试
- **线程安全**：多线程架构，控制、状态监听、SPI 处理各自独立

## 系统架构

```
┌─────────────────────────────────────────────────────┐
│                   IO2HTTP Daemon                     │
│                                                       │
│  ┌──────────────┐  ┌──────────┐  ┌────────────────┐ │
│  │  Unix Socket  │  │   HTTP   │  │   WebSocket    │ │
│  │  (控制/状态)   │  │   REST   │  │   (状态推送)    │ │
│  └──────┬───────┘  └────┬─────┘  └───────┬────────┘ │
│         │               │                │           │
│  ┌──────┴───────────────┴────────────────┴───────┐   │
│  │              GPIO 控制核心                      │   │
│  │    命令解析 / 状态缓存 / SPI 队列管理             │   │
│  └──────────────────────┬────────────────────────┘   │
│                         │                             │
│  ┌──────────────────────┴────────────────────────┐   │
│  │              USB GPIO 控制器                     │   │
│  │       串口通信 / 指令编码 / 状态轮询              │   │
│  └──────────────────────┬────────────────────────┘   │
└─────────────────────────┼───────────────────────────┘
                          │
                  ┌───────┴───────┐
                  │  USB2GPIO 设备  │
                  │ (BL-ENV-V1.3)  │
                  └───────────────┘
```

## 快速开始

### 配置

编辑 `config/config.ini`，配置 GPIO 设备和接口参数：

```ini
[daemon_config]
socket_path = /tmp/gpio.sock       # 控制命令 Socket
get_statu_path = /tmp/gpio_get.sock # 状态监听 Socket
http_port = 8080                    # HTTP 服务端口（0=禁用）
ws_port = 8081                      # WebSocket 服务端口（0=禁用）
```

## 安装

### 依赖

- Python 3
- `python-snap7` — 西门子 S7 PLC 通信（S7 协议支持时需要）
- `pyserial` — 串口通信
- `websocket-client` — WebSocket 客户端（可选）

完整依赖列表见 [requirements.txt](requirements.txt)。

### 虚拟环境

```bash
# 创建虚拟环境
python3 -m venv .venv

# 安装依赖（Ubuntu，需先创建 venv）
.venv/bin/pip install -r requirements.txt

# 安装依赖（Fedora）
pip install -r requirements.txt
```

### 启动

```bash
# 激活虚拟环境（可选）
source .venv/bin/activate

# 模拟模式（无需硬件）
python3 daemon_gpio.py --simulate

# 生产模式
python3 daemon_gpio.py

# 调试模式
python3 daemon_gpio.py --debug

# 调试 SPI
python3 daemon_gpio.py --debug-spi
```

### 使用脚本

```bash
./start_daemon.sh    # 启动（模拟模式）
./stop_daemon.sh     # 停止并清理
```

### 运行测试

```bash
python3 test_daemon.py    # 功能测试
python3 test_fix.py       # 集成测试
```

## 配置说明

### GPIO 设备配置

| 参数 | 说明 | 示例 |
|------|------|------|
| `tty_path` | 串口设备路径 | `/dev/ttyUSB0` |
| `baudrate` | 串口波特率 | `115200` |
| `alias` | 设备别名，用于命令寻址 | `sender` |
| `mode` | 操作模式：`seter`/`geter`/`spi` | `seter` |
| `lag_time` | SPI 操作间隔（毫秒） | `1` |

### S7 PLC 设备配置

S7 PLC 是双工设备，配置中无需指定 `mode`，通过 JSON 命令的 `mode` 决定读写操作：

| 参数 | 说明 | 示例 |
|------|------|------|
| `protocol` | 协议类型：`usb2gpio` 或 `s7` | `s7` |
| `ip_address` | PLC IP 地址 | `192.168.1.8` |
| `port` | S7 端口（默认 102） | `102` |
| `rack` | 机架号（默认 0） | `0` |
| `slot` | 槽号（S7-1200/1500 通常为 1） | `1` |
| `alias` | 设备别名，用于命令寻址 | `plc` |

### 三种工作模式

- **seter（输出控制）**：控制继电器、门锁、灯泡等输出设备
- **geter（输入读取）**：读取传感器、按钮等输入设备状态
- **spi（SPI 通信）**：基于 bit-banging 的低速 SPI，通过 `clk`/`data` 引脚和 `cs_1`~`cs_14` 片选线实现

### S7 PLC 控制

S7 PLC 使用西门子 PLC 地址格式（如 `Q0.0`, `I1.2`, `M10.5`）：

| IO 类型 | 说明 | 地址示例 |
|---------|------|----------|
| Q | 输出过程映像（控制继电器等） | `Q0.0`, `Q0.1`, `Q1.2` |
| I | 输入过程映像（读取传感器等） | `I0.0`, `I1.2` |
| M | 存储器区（中间状态等） | `M0.0`, `M10.5` |

**控制示例**：

```json
{
  "alias": "plc",
  "mode": "seter",
  "gpio": "Q0.0",
  "value": 1
}
```

```bash
# 写入 PLC 输出（seter）
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "plc", "mode": "seter", "gpio": "Q0.0", "value": 1}'

# 读取 PLC 状态（geter）
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "plc", "mode": "geter", "gpio": "I0.0"}'

# 批量控制
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "plc", "mode": "seter", "gpios": ["Q0.0", "Q0.1"], "values": [1, 0]}'
```

## 接口文档

### Unix Socket 控制

通过 `SOCK_DGRAM` 向 `/tmp/gpio.sock` 发送 JSON 命令：

```json
{
  "alias": "sender",
  "mode": "set",
  "gpio": 1,
  "value": 1
}
```

### HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/gpio` | GPIO 控制（单路/批量/SPI） |
| `GET` | `/status` | 查询 GPIO 状态 |

#### 示例

```bash
# 控制单路 GPIO
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "sender", "mode": "set", "gpio": 1, "value": 1}'

# 查询状态
curl http://localhost:8080/status
```

### WebSocket 实时推送

连接 `ws://localhost:8081`，GPIO 状态变化时服务器主动推送 JSON 消息。

### Unix Socket 状态监听

连接 `/tmp/gpio_get.sock`（SOCK_STREAM），接收 GPIO 状态变化推送，支持 `query_status` 主动查询和 `ack` 确认机制。

### 命令格式详情

详细命令格式说明请参考：

- [命令格式](docs/command_format.md) — 单路/批量 GPIO 及 SPI 命令格式
- [HTTP API](docs/http_api.md) — HTTP 和 WebSocket 接口文档
- [状态格式](docs/status_format.md) — GPIO 状态消息格式及交互协议
- [实现原理](docs/implementation.md) — 系统架构和通信机制设计

## 主从架构

主机运行 IO2HTTP 守护进程，从机通过 HTTP 接口远程控制：

```python
import requests
requests.post('http://192.168.1.100:8080/gpio', json={
    'alias': 'sender', 'mode': 'set', 'gpio': 5, 'value': 1
})
```

## 项目结构

```
├── daemon_gpio.py              # 入口脚本
├── config/
│   └── config.ini              # 配置文件
├── gpio/
│   ├── base_controller.py      # 控制器基类（定义统一接口）
│   ├── controller.py           # USB2GPIO 控制器（串口通信）
│   ├── s7plc_controller.py     # S7 PLC 控制器（西门子 S7 通信）
│   ├── daemon.py               # 守护进程核心逻辑
│   ├── server_http.py          # HTTP 服务
│   └── server_ws.py            # WebSocket 服务
├── docs/
│   ├── command_format.md       # 命令格式说明
│   ├── http_api.md             # HTTP API 文档
│   ├── implementation.md       # 实现原理
│   └── status_format.md        # 状态格式说明
├── debug_utils/
│   ├── s7_test.py              # S7 PLC 通信测试
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

## 依赖

- Python 3
- `pyserial` — 串口通信
- `websocket-client` — WebSocket 客户端（可选）
- `python-snap7` — 西门子 S7 PLC 通信（S7 协议支持时需要）

## 许可证

[MIT](LICENSE)

Copyright (c) 2026 新毛宝贝
