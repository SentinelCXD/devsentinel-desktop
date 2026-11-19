export type Message = {
  role: 'system' | 'user' | 'assistant';
  content: string;
};

// 后端约定的错误结构：SSE 的 error 事件和 /chat_once 的错误 body 都是这个形状，
// 前端只认 code 做分支，message 做展示，retryable 决定要不要显示「重试」按钮
export interface LLMErrorInfo {
  code: string;      // 'auth' | 'rate_limit' | 'upstream' | 'timeout' | 'connection' | 'bad_request' | ...
  message: string;
  retryable: boolean;
}

// token 用量：usage 事件和 UsageChips 组件共用
export type Usage = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
};

// 后端 SSE 事件的完整协议，用联合类型建模。
// 所有 data: 都是 JSON，按 evt.type 分发，不再混裸字符串。
export type StreamEvent =
  // 模型正文增量：流式逐段到达，前端累积渲染（打字机效果）
  | { type: 'content'; text: string }

  // 推理模型的思考过程增量：仅 deepseek-reasoner 产生，
  // 只用于 UI 展示，绝不能写回消息历史（会污染上下文还烧 token）
  | { type: 'reasoning'; text: string }

  // token 用量：流式时最后一个内容事件之后到达（stream_options include_usage），
  // 非流式时在响应 JSON 里，不走这个通道
  | {
    type: 'usage';
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  }

  // 业务错误：流内错误的唯一表达方式（HTTP 200 已发出，只能走事件）。
  // retryable 决定是否显示重试按钮
  | ({ type: 'error' } & LLMErrorInfo)
  
  // 流结束标记：代替旧的 "[DONE]" 裸字符串，
  // 以后可扩展 finish_reason / steps 等元数据
  | { type: 'done' }

  // 工作流步骤事件：告诉前端流水线走到哪一步了
  | { type: 'step'; name: string; label: string }

  // 工作流步骤结果：某一步的产出（提取的要素 / grep 到的日志），前端可折叠展示
  | { type: 'step_result'; name: string; data: unknown }

  // Agent 工具调用：模型决定调哪个工具、传什么参数。
  // args 后端目前发的是模型原样给的 JSON 字符串（tc.function.arguments），也兼容对象；
  // id 是 tool_call_id，带上后前端按它配对 tool_result，并行同名调用也不会错位
  | { type: 'tool_call'; name: string; args: string | Record<string, unknown>; id?: string }

  // Agent 工具结果：工具执行返回的内容
  | { type: 'tool_result'; name: string; result: string; id?: string }

  // Agent 每轮调工具前模型说的话（msg.content），时间线里显示为这一步的思考
  | { type: 'thought'; text: string }

  // 人工审批（HITL）：write/danger 级工具执行前挂起，等用户决定。
  // 前端收到后弹审批框；用户点批准/拒绝 → POST /api/agent/approve，
  // 后端 resolve_approval 唤醒 agent_loop：批准则执行，拒绝则错误 observation 回传模型
  | {
      type: 'approval_required';
      approval_id: string;                          // 决议时回传的 id
      tool_call_id?: string;                        // 配对时间线上的工具项
      name: string;                                 // 待审批的工具名
      args: string | Record<string, unknown>;      // 模型给的参数（审批时要看清楚再点）
      level: 'write' | 'danger';                    // 风险等级
    }

// 结构化诊断结果：后端 DiagnosisResult 的镜像，前端按字段渲染卡片而不是整段文本
export interface DiagnosisResult {
  root_cause: string;
  confidence: number;          // 0-1
  evidence: string[];
  suggested_actions: string[];
}

export interface FaultInfo {
  service: string;
  error_type: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  keywords: string[];
}
