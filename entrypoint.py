#!/usr/bin/env python3
"""容器入口：TOKENLENS_SOURCE=local|demo，TOKENLENS_HOST/TOKENLENS_PORT 覆盖监听地址。"""

import os
import sys

sys.path.insert(0, "backend")

import server  # noqa: E402

if __name__ == "__main__":
    server.main()
