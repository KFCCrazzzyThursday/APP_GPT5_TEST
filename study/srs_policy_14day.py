# -*- coding: utf-8 -*-
"""
SRS 策略参数（集中管理，便于调参）
"""
POLICY = {
    # EMA 强度分：score = (1 - alpha) * old + alpha * outcome(0/1)
    "ema_alpha": 0.30,

    # 艾宾浩斯保留率的时间尺度参数 τ（天）
    "tau_days": 1.6,

    # SM-2 初始/上下限
    "sm2_init_ease": 2.5,
    "sm2_min_ease": 1.3,
    "sm2_max_ease": 3.0,

    # 错误/正确时对 ease 的微调（会再受 score 影响）
    "delta_ease_wrong": 0.20,
    "delta_ease_right": 0.10,

    # 新卡前两步的基础间隔（天）
    "first_interval_days": 1.0,
    "second_interval_days": 6.0,

    # 过期复习的奖励（把实际间隔乘以 overdue_factor）
    "overdue_factor": 1.15
}
