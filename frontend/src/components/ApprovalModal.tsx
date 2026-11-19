import type { StreamEvent } from '../type';

// 审批事件：从 StreamEvent 里抽出来，App 持有同款状态传进来
export type ApprovalEvent = Extract<StreamEvent, { type: 'approval_required' }>;

// 风险等级徽章：write 黄色（有副作用），danger 红色（高危）
const LEVEL_BADGE: Record<ApprovalEvent['level'], { label: string; cls: string }> = {
  write: { label: 'write · 有副作用', cls: 'bg-amber-900/70 text-amber-300 border-amber-700/60' },
  danger: { label: 'danger · 高危操作', cls: 'bg-red-900/70 text-red-300 border-red-700/60' },
};

// 参数是 JSON 字符串就格式化成多行，解析失败原样展示（与 Timeline 的 prettyArgs 同约定）
function prettyArgs(args: string | Record<string, unknown>): string {
  return typeof args === 'string' ? (() => {
    try {
      return JSON.stringify(JSON.parse(args), null, 2);
    } catch {
      return args;
    }
  })() : JSON.stringify(args, null, 2);
}

// 人工审批弹窗：Agent 想执行 write/danger 级工具时挂起等决定。
// 不点「批准」绝不执行；「拒绝」后后端把错误 observation 喂回模型继续排查
export default function ApprovalModal({ approval, onResolve }: {
  approval: ApprovalEvent;
  onResolve: (approved: boolean) => void;
}) {
  const badge = LEVEL_BADGE[approval.level];
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="mx-4 w-full max-w-lg rounded-xl border border-slate-700 bg-slate-900 shadow-2xl">
        <div className="flex items-center gap-2 border-b border-slate-800 px-5 py-3">
          <span className="text-amber-400">⚠️</span>
          <span className="text-sm font-semibold text-slate-100">工具执行审批</span>
          <span className={`ml-auto rounded border px-2 py-0.5 text-[10px] ${badge.cls}`}>{badge.label}</span>
        </div>

        <div className="space-y-3 px-5 py-4">
          <p className="text-xs text-slate-400">
            Agent 请求执行
            <code className="mx-1 rounded bg-slate-800 px-1.5 py-0.5 font-mono text-amber-300">{approval.name}</code>
            ，该工具为 <code className="font-mono text-slate-300">{approval.level}</code> 级，需要你确认后才会执行。
          </p>
          <div>
            <div className="mb-1 text-xs text-slate-500">参数</div>
            <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 font-mono text-xs text-slate-300 border border-slate-800">
              {prettyArgs(approval.args)}
            </pre>
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-slate-800 px-5 py-3">
          <button
            type="button"
            onClick={() => onResolve(false)}
            className="rounded-md bg-slate-700 px-4 py-1.5 text-xs text-slate-200 transition hover:bg-slate-600"
          >
            拒绝
          </button>
          <button
            type="button"
            onClick={() => onResolve(true)}
            className="rounded-md bg-amber-600 px-4 py-1.5 text-xs font-medium text-white transition hover:bg-amber-500"
          >
            批准执行
          </button>
        </div>
      </div>
    </div>
  );
}
