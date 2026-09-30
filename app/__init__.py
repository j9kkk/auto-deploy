"""AutoDeploy — automatic GitHub-to-Linux deploy service."""

# 版本号唯一来源：CLI、OpenAPI、健康检查与统计接口全部从此处读取，
# 避免多处各自维护导致漂移（曾出现 API 报 1.0.0 而 CLI 报 1.0.1）。
__version__ = "1.5.1"
