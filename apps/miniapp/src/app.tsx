import { CHILD_FLOW, PARENT_FLOW, canChildSeePaymentControls } from "./domain/taskLoop";

export const APP_CONTRACT = {
  launchSurface: "wechat-mini-program-mvp",
  modes: ["child", "parent"] as const,
  childFlow: CHILD_FLOW,
  parentFlow: PARENT_FLOW,
  paymentControlsVisibleToChild: canChildSeePaymentControls(),
  externalEffects: "disabled-in-offline-slice",
} as const;

export default APP_CONTRACT;
