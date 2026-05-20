# GPIO Daemon HTTP 接口文档

## 概述

GPIO 守护进程支持通过 HTTP 接口远程控制 GPIO，JSON 格式与 Unix Socket 完全一致，便于主从机架构部署。

## 基础配置

编辑 `deamon/daemon_gpio/config/config.ini`：

```ini
[daemon_config]
# HTTP 控制接口端口（0 = 禁用）
http_port = 8080
# WebSocket 状态推送端口（0 = 禁用）
ws_port = 8081
```

## HTTP API

### 1. GPIO 控制

**POST** `/gpio`

发送 GPIO 控制命令。

**请求体**（JSON）：
```json
{
  "alias": "sender",
  "mode": "set",
  "gpio": 1,
  "value": 1
}
```

**响应**：
```json
{"success": true, "alias": "sender", "gpio": 1, "value": 1}
```

**批量设置**：
```json
{
  "alias": "sender",
  "mode": "set",
  "gpios": [1, 2, 3],
  "values": [1, 0, 1]
}
```

### 2. SPI 通信

**POST** `/gpio`

```json
{
  "alias": "spi",
  "mode": "spi",
  "spi_num": 1,
  "spi_data": "10000100",
  "spi_data_cs_collection": "down"
}
```

**多路 SPI**：
```json
{
  "alias": "spi",
  "mode": "spi_multi",
  "spis": [
    {"spi_num": 1, "spi_data": "10000100"},
    {"spi_num": 2, "spi_data": "11110000"}
  ]
}
```

### 3. 查询状态

**GET** `/status`

查询所有 geter 模式 GPIO 的当前状态。

**响应**：
```json
{
  "type": "current_status",
  "timestamp": 1234567890.123,
  "gpios": [
    {
      "alias": "geter",
      "default_bit": 0,
      "current_gpio_states": {"1": 0, "2": 1, "3": 0}
    }
  ]
}
```

## WebSocket API

> 详细的事件驱动监听文档见 [WebSocket 事件监听](websocket.md)。

连接地址：`ws://<host>:<ws_port>/`，GPIO 状态变化时服务器主动推送 JSON 消息。

## 使用示例

### curl

```bash
# === USB2GPIO ===

# 设置单个 GPIO
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "sender", "mode": "set", "gpio": 1, "value": 1}'

# 批量设置
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "sender", "mode": "set", "gpios": [1, 2], "values": [1, 0]}'

# 查询状态
curl http://localhost:8080/status

# === S7 PLC ===

# 写入 PLC 输出（拉起 Q0.0 继电器）
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "plc", "mode": "seter", "gpio": "Q0.0", "value": 1}'

# 读取 PLC 输入
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "plc", "mode": "geter", "gpio": "I0.0"}'

# 批量控制（同时操作 Q0.0 和 Q0.1）
curl -X POST http://localhost:8080/gpio \
  -H "Content-Type: application/json" \
  -d '{"alias": "plc", "mode": "seter", "gpios": ["Q0.0", "Q0.1", "Q0.2"], "values": [1, 0, 1]}'
```

### Python

```python
import requests

# USB2GPIO
requests.post('http://localhost:8080/gpio', json={
    'alias': 'sender', 'mode': 'set', 'gpio': 1, 'value': 1
})

# S7 PLC
requests.post('http://localhost:8080/gpio', json={
    'alias': 'plc', 'mode': 'seter', 'gpio': 'Q0.0', 'value': 1
})

# 查询状态
status = requests.get('http://localhost:8080/status').json()
```

### JavaScript + WebSocket

```javascript
const ws = new WebSocket('ws://localhost:8081');

ws.onopen = () => {
    console.log('WebSocket 已连接');
};

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'gpio_change') {
        console.log('GPIO 变化:', data.gpios);
    }
};
```

## 错误响应

| HTTP 状态码 | 说明 |
|-------------|------|
| 200 | 成功 |
| 400 | JSON 格式错误 |
| 500 | 服务器内部错误 |

**错误格式**：
```json
{"error": "Unknown alias: invalid", "available": ["sender", "spi", "geter"]}
```

## 主从机架构示例

**主机（直接控制）**：
```bash
python3 daemon_gpio.py --simulate
```

**从机（HTTP 控制主机）**：
```python
import requests

# 通过 HTTP 控制主机 GPIO
requests.post('http://192.168.1.100:8080/gpio', json={
    'alias': 'sender',
    'mode': 'set',
    'gpio': 5,
    'value': 1
})
```

## 依赖

```bash
pip install websocket-client
```
