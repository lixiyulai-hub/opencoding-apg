"""Enable the required default unittest discovery for the integrated project."""

# N01-a：本套件不再设置任何进程级豁免环境变量。
# 受信任合成测试通过显式参数 `trusted_fixture` 绑定已审查夹具身份
# （见 REVIEWED_FIXTURE 与各测试模块的 run_batch 包装），普通 CLI 与
# 真实模型路径不会因导入测试、环境继承或旧会话设置获得豁免。
REVIEWED_FIXTURE = "tests-reviewed-synthetic-v1"
