"""pytest 根配置：把 backend/ 加入 sys.path，使测试可直接 import app.*。

纯函数级单测，不连接真实数据库 / 不依赖硬件。
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
