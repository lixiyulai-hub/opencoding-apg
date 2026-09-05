export type Mode = "child" | "parent";
export type TaskState = "assigned" | "accepted" | "awaiting-review" | "returned" | "completed" | "repairable";

export interface TaskEvent { name: string; actor: Mode; }
export interface TaskSnapshot { id: string; state: TaskState; events: TaskEvent[]; balance: number; whyAnswered: boolean; }

export const CHILD_FLOW = ["view-task", "complete-and-submit-proof", "answer-why"] as const;
export const PARENT_FLOW = ["review-proof", "approve-or-return", "manage-contract"] as const;

export function canChildSeePaymentControls(): boolean { return false; }
export function createTask(id: string): TaskSnapshot {
  if (!id.trim()) throw new Error("task id required");
  return { id, state: "assigned", events: [], balance: 0, whyAnswered: false };
}
export function childAccept(task: TaskSnapshot): TaskSnapshot {
  if (task.state !== "assigned") throw new Error("invalid transition");
  return { ...task, state: "accepted", events: [...task.events, { name: "accepted", actor: "child" }] };
}
export function childSubmitProof(task: TaskSnapshot): TaskSnapshot {
  if (task.state !== "accepted") throw new Error("invalid transition");
  return { ...task, state: "awaiting-review", events: [...task.events, { name: "proof_submitted", actor: "child" }] };
}
export function parentReview(task: TaskSnapshot, approve: boolean): TaskSnapshot {
  if (task.state !== "awaiting-review") throw new Error("invalid transition");
  return approve
    ? { ...task, state: "completed", balance: task.balance + 1, events: [...task.events, { name: "parent_approved", actor: "parent" }] }
    : { ...task, state: "returned", events: [...task.events, { name: "parent_returned", actor: "parent" }] };
}
