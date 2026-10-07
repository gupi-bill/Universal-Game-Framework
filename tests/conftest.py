"""pytest 公共配置：把仓库根目录加入 import 路径，并强制离线模式。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 无头 / CI 环境：全程离线，不碰真实键鼠与外部服务
os.environ.setdefault("UGF_DRY_RUN", "1")
os.environ.setdefault("UGF_PERCEPTION_BACKEND", "mock")
