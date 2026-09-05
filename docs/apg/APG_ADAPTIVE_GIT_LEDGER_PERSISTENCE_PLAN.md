# APG Ledger 持久化阶段计划

## 范围
1. 生成确定性写入计划；2. Gate-P1 写前校验；3. Gate-P2 独立副本原子替换与 rollback；4. Gate-P3 测试、重放、证据哈希和独立复核。

## Gate
- Gate-P1：schema、scope、snapshot/preimage hash、唯一事件、目标 containment 全部通过。
- Gate-P2：临时文件 + 原子替换、幂等重放、失败冻结、独立副本 rollback。
- Gate-P3：全量 unittest、独立 review、RESULT/DIFF/VERIFICATION 哈希一致，external_actions 仅记录本地写入观察。

## 边界
不连接任何执行器或网络，不运行 runtime，不部署/发布，不执行真实 Git。后续真实 Git checkpoint 为独立事务。
