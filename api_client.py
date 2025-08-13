# -*- coding: utf-8 -*-
import os
import httpx
from openai import OpenAI
from config import API_KEY, BASE_URL, DEFAULT_TIMEOUT

def setup_client():
    # 清理代理，避免连不上
    for var in ("ALL_PROXY", "all_proxy"):
        os.environ.pop(var, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "api.nuwaapi.com"
    http_client = httpx.Client(http2=False, proxy=None, timeout=DEFAULT_TIMEOUT)
    return OpenAI(api_key=API_KEY, base_url=BASE_URL, http_client=http_client)
