"""RCCP — 粗能力计划（Rough-Cut Capacity Planning）后端服务。

模块划分：
- config       服务配置（计划期周数、数据库路径）
- errors       领域异常
- db           SQLite 连接与建表
- bom          资源清单（产品→工作中心 单件工时/提前量）及其版本
- capacity     能力日历（工作中心×周 可用工时）及其版本
- explosion    负荷全量展开（纯函数）
- incremental  草稿负荷矩阵的增量维护
- plans        计划草稿、已发布计划版本、负荷结果持久化
- compare      超负荷分析与负荷结果对比
- service      应用服务层（装配以上模块，持有内存态与写锁）
- schemas      API 请求模型
- api          FastAPI 路由
- main         应用工厂
"""

__version__ = "1.0.0"
