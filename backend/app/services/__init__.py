"""业务编排层：把 schema / codegen / executor / verifier / LLM 与数据库串起来。

API 路由只做参数校验与依赖注入，实际流程都在 service 中完成。
"""
