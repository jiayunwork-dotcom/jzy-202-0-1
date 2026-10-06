"""uvicorn 入口：uvicorn rccp.asgi:app

独立成模块是为了让 `import rccp.main`（测试用 create_app）不带
"按环境变量建库"的副作用。
"""
from .config import Settings
from .main import create_app

app = create_app(Settings.from_env())
