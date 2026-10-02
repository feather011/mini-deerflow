"""模型调用领域异常（21A）。

职责：
- 把供应商 SDK 的 HTTP/网络错误，收敛成前端与日志都能稳定依赖的领域异常
- 异常消息只包含状态码、供应商、模型名等非敏感信息，绝不携带 API Key

异常层级：

    ModelError
    ├── ModelConfigError       配置缺失/非法（fail-closed，不降级）
    ├── ModelTimeoutError      请求超时
    ├── ModelAuthError         401/403 鉴权失败
    ├── ModelRateLimitError    429 限流
    ├── ModelServerError       5xx / 连接失败
    ├── ModelEmptyResponseError 200 但没有可读的文本内容
    └── ModelProtocolError     模型响应不符合约定（21B 工具闭环）
"""

from __future__ import annotations


class ModelError(Exception):
    """模型调用失败的基类。"""


class ModelConfigError(ModelError):
    """模型配置缺失或非法。启动期抛出，禁止静默降级。"""


class ModelTimeoutError(ModelError):
    """模型请求超时。"""


class ModelAuthError(ModelError):
    """鉴权失败（401/403）。"""


class ModelRateLimitError(ModelError):
    """限流（429）。"""


class ModelServerError(ModelError):
    """模型服务端错误（5xx）或无法建立连接。"""


class ModelEmptyResponseError(ModelError):
    """响应成功但没有可读的文本内容。"""


class ModelProtocolError(ModelError):
    """模型响应结构不符合约定（21B）。

    典型场景：
    - 一次返回多个 tool_use（当前 Agent 只支持串行单工具）
    - tool_use 缺少 id / name
    - tool_use.input 不是 JSON object
    - tool_use.input 缺少 schema 声明的必填字段

    这类错误**不得静默降级**（例如丢掉多余的 tool_use 硬凑一次调用），
    否则模型与实际执行的工具会对不上，产生无法排查的错误结果。
    """
