"""pytest 引导：把 backend 根目录加入 sys.path，使 `import app.xxx` 可用。

这样无论从哪个目录启动 pytest，测试都能正常导入应用代码。
"""

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
