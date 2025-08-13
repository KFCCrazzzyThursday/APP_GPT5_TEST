# -*- coding: utf-8 -*-
import os

# === 建议：优先用环境变量设置 ===
#  PowerShell:
#   $env:NUWA_API_KEY="sk-xxxxxxxx"
#  Linux/macOS:
#   export NUWA_API_KEY="sk-xxxxxxxx"
API_KEY = os.getenv(
    "NUWA_API_KEY") or "sk-Hl2PVAQEgU0hHMy2bLHnrPgrm6T4j58qK9Av9uKBnpN3iq8L"

BASE_URL = "https://api.nuwaapi.com/v1"
MODEL_NAME = "gpt-5-chat"

# 并发默认值（可在 GUI/CLI 覆盖）
DEFAULT_BATCH_SIZE = 4
DEFAULT_CHECKPOINT_EVERY = 20
DEFAULT_TIMEOUT = 120
