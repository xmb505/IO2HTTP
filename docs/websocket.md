# WebSocket 事件驱动监听文档

## 概述

WebSocket 是 IO2HTTP 的**事件驱动核心通道**。与 HTTP 的请求-响应模式不同，WebSocket 连接建立后由服务器主动推送 GPIO 状态变化，客户端无需轮询，适用于实时监控、仪表盘、告警等场景。

```
┌──────────────┐                          ┌──────────────┐
│   IO2HTTP    │   ws://localhost:8081    │   客户端       │
│   Daemon     │◄─────── 握手 ──────────►│   (浏览器/     │
│              │                          │    Python/     │
│  GPIO 变化   │──── 主动推送 JSON ──────►│    Node.js)    │
│  PLC 输入变化 │──── 主动推送 JSON ──────►│              │
└──────────────┘                          └──────────────┘
```

### 三种接口对比

| 接口 | 模式 | 用途 |
|------|------|------|
| **HTTP POST /gpio** | 请求-响应 | 主动控制 GPIO / 查询状态 |
| **WebSocket** | 服务器推送 | **事件驱动实时监听**（推荐） |
| **Unix Socket 状态监听** | 服务器推送 | 同 WebSocket，本地进程间通信 |

## 连接方式

```
ws://<host>:<ws_port>
```

- `ws_port` 在 `config/config.ini` 中配置，默认 `8081`
- 无需指定路径，连接即开始握手
- 支持多个客户端同时连接

```javascript
// JavaScript 浏览器
const ws = new WebSocket('ws://192.168.1.100:8081');
```

```python
# Python
import websocket
ws = websocket.create_connection('ws://localhost:8081')
```

## 消息格式

### GPIO 状态变化推送（`gpio_change`）

当任何 GPIO 输入状态发生变化时，服务器主动推送。**单条消息可包含多个设备、多个引脚的同时变化**。

#### USB2GPIO 输入变化

```json
{
  "type": "gpio_change",
  "id": 42,
  "timestamp": 1747728000.123,
  "gpios": [
    {
      "alias": "geter",
      "default_bit": 0,
      "change_gpio": [
        {"gpio": 1, "bit": 0},
        {"gpio": 3, "bit": 1}
      ]
    }
  ]
}
```

#### S7 PLC 输入变化

```json
{
  "type": "gpio_change",
  "id": 43,
  "timestamp": 1747728000.456,
  "gpios": [
    {
      "alias": "plc",
      "default_bit": 0,
      "change_gpio": [
        {"gpio": "I0.0", "bit": 1},
        {"gpio": "I0.1", "bit": 0},
        {"gpio": "I1.2", "bit": 1}
      ]
    }
  ]
}
```

#### 混合推送（多个设备同时变化）

```json
{
  "type": "gpio_change",
  "id": 44,
  "timestamp": 1747728000.789,
  "gpios": [
    {
      "alias": "geter",
      "default_bit": 0,
      "change_gpio": [
        {"gpio": 5, "bit": 1}
      ]
    },
    {
      "alias": "plc",
      "default_bit": 0,
      "change_gpio": [
        {"gpio": "I3.4", "bit": 1},
        {"gpio": "I3.5", "bit": 0}
      ]
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"gpio_change"` |
| `id` | int | 消息序号，单调递增 |
| `timestamp` | float | Unix 时间戳（秒） |
| `gpios` | array | 变化的设备列表 |
| `gpios[].alias` | string | 设备别名（如 `geter`, `plc`） |
| `gpios[].default_bit` | int | 查询电平指令集（USB2GPIO 专用） |
| `gpios[].change_gpio` | array | 变化的引脚列表 |
| `change_gpio[].gpio` | int/string | 引脚编号（USB2GPIO: 数字，PLC: `"I0.0"`） |
| `change_gpio[].bit` | int | 当前状态：0 或 1 |

## 事件驱动编程模型

### JavaScript 浏览器

```javascript
let gpioStateCache = {};  // 本地状态缓存

const ws = new WebSocket('ws://localhost:8081');

ws.onopen = () => {
    console.log('[WebSocket] 已连接');
};

ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === 'gpio_change') {
        for (const device of msg.gpios) {
            for (const change of device.change_gpio) {
                const key = `${device.alias}:${change.gpio}`;
                gpioStateCache[key] = change.bit;

                // 事件驱动：根据变化执行不同逻辑
                if (change.bit === 1) {
                    onInputRising(device.alias, change.gpio);
                } else {
                    onInputFalling(device.alias, change.gpio);
                }
            }
        }
    }
};

ws.onclose = () => {
    console.log('[WebSocket] 断开，3秒后重连...');
    setTimeout(connect, 3000);
};

ws.onerror = (err) => {
    console.error('[WebSocket] 错误:', err);
};

function onInputRising(alias, gpio) {
    // 上升沿处理：例如 I0.0 变为 1 → 传感器触发
    console.log(`↑ ${alias}:${gpio} 变为高电平`);
    if (alias === 'plc' && gpio === 'I0.0') {
        // 具体业务逻辑
        handleSensorTrigger();
    }
}

function onInputFalling(alias, gpio) {
    console.log(`↓ ${alias}:${gpio} 变为低电平`);
}
```

### Python

```python
import json
import websocket
import threading
import time

class GPIOMonitor:
    def __init__(self, ws_url='ws://localhost:8081'):
        self.ws_url = ws_url
        self.ws = None
        self.state_cache = {}

    def on_message(self, ws, message):
        msg = json.loads(message)
        if msg.get('type') == 'gpio_change':
            for device in msg['gpios']:
                for change in device['change_gpio']:
                    key = f"{device['alias']}:{change['gpio']}"
                    old = self.state_cache.get(key)
                    new = change['bit']
                    self.state_cache[key] = new

                    if old is not None and old != new:
                        direction = '↑' if new == 1 else '↓'
                        print(f'{direction} {key} = {new}')

    def on_error(self, ws, error):
        print(f'[WebSocket] 错误: {error}')

    def on_close(self, ws, close_status_code, close_msg):
        print('[WebSocket] 断开，5秒后重连...')
        time.sleep(5)
        self.connect()

    def on_open(self, ws):
        print('[WebSocket] 已连接')

    def connect(self):
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def get_state(self, alias, gpio):
        """查询缓存的状态"""
        return self.state_cache.get(f'{alias}:{gpio}')


# 使用
monitor = GPIOMonitor('ws://localhost:8081')
monitor.connect()

# 保持主线程运行
while True:
    time.sleep(1)
```

### Node.js

```javascript
const WebSocket = require('ws');

function connect() {
    const ws = new WebSocket('ws://localhost:8081');

    ws.on('open', () => {
        console.log('[WebSocket] 已连接');
    });

    ws.on('message', (data) => {
        const msg = JSON.parse(data);
        if (msg.type === 'gpio_change') {
            msg.gpios.forEach(device => {
                device.change_gpio.forEach(change => {
                    console.log(`[${device.alias}] ${change.gpio} = ${change.bit}`);
                });
            });
        }
    });

    ws.on('close', () => {
        console.log('[WebSocket] 断开，3秒后重连...');
        setTimeout(connect, 3000);
    });

    ws.on('error', (err) => {
        console.error('[WebSocket] 错误:', err.message);
    });
}

connect();
```

## 断线重连策略

WebSocket 连接可能因网络问题断开，建议实现指数退避重连：

```javascript
let reconnectDelay = 1000;  // 初始 1 秒
const maxDelay = 30000;     // 最大 30 秒

function connect() {
    const ws = new WebSocket('ws://localhost:8081');

    ws.onopen = () => {
        reconnectDelay = 1000;  // 重置延迟
        console.log('已连接');
    };

    ws.onclose = () => {
        console.log(`断开，${reconnectDelay / 1000}秒后重连...`);
        setTimeout(() => {
            connect();
            reconnectDelay = Math.min(reconnectDelay * 2, maxDelay);
        }, reconnectDelay);
    };
}
```

## 与 Unix Socket 状态监听的对比

| 特性 | WebSocket | Unix Socket |
|------|-----------|-------------|
| 协议 | WebSocket (RFC 6455) | 自定义 TCP (JSON) |
| 跨网络 | 支持 | 仅本机 |
| 浏览器支持 | 原生支持 | 不支持 |
| 消息格式 | 相同 JSON | 相同 JSON |
| ACK 确认 | 不支持 | 支持 `{"type":"ack"}` |
| 状态查询 | 不支持 | 支持 `{"type":"query_status"}` |

> 如果只需要本机进程间通信且需要 ACK 确认 + 主动查询，使用 Unix Socket。其他场景推荐 WebSocket。

## 消息合并机制

为提高效率，短时间内多次 GPIO 变化会被合并为一条消息推送。合并窗口为 **50ms**（`gpio_change_buffer_send_interval`）。

这意味着：

```
t=0ms   I0.0 变为 1
t=10ms  I0.1 变为 1
t=20ms  I1.2 变为 0
t=50ms  ─── 合并发送 ──→ WebSocket 客户端收到一条包含 3 个变化的消息
```

客户端收到的是**一次连接中的所有位图变化**，PLC 的 100 字节位图本身就是批量的，一次可能包含多个 IO 变化。
